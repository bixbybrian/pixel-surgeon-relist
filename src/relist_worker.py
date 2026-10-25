"""
Weekly sweep: find listings older than AGE_THRESHOLD_DAYS, refresh up to
WEEKLY_REFRESH_CAP of them (oldest first), and write the updated database
back to disk.

Run modes:
  --dry-run   Always safe. Prints exactly what would change; never calls
              eBay; still updates data/listings.json's "history" only if
              you also pass --write (so you can preview without touching
              the file at all).
  (default)   Requires real eBay credentials in the environment. Calls
              ebay_client.end_and_relist for real.

Usage:
  python -m src.relist_worker --dry-run
  python -m src.relist_worker --dry-run --write
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from . import config, ebay_client, title_rules

UTC = timezone.utc


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(UTC)


def load_db(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(path: str, db: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
        f.write("\n")


def select_stale_listings(db: dict, now: datetime) -> list[dict]:
    threshold = timedelta(days=config.AGE_THRESHOLD_DAYS)
    eligible = []
    for listing in db["listings"]:
        # needs_review=True means sync_inventory.py couldn't confidently
        # parse a platform/condition for this listing (e.g. it isn't a game
        # at all, or uses phrasing the title parser doesn't recognize yet).
        # Permanently out of scope for the automated sweep until a human
        # clears the flag by hand in the database file - never auto-touched
        # on the strength of a guess.
        if listing.get("needs_review"):
            continue
        anchor = listing.get("last_refreshed_date") or listing["first_listed_date"]
        age = now - _parse_dt(anchor)
        if age >= threshold:
            eligible.append((age, listing))
    # Oldest (largest age) first.
    eligible.sort(key=lambda pair: pair[0], reverse=True)
    return [listing for _age, listing in eligible[: config.REFRESH_CAP_PER_RUN]]


def pick_description_append(listing: dict) -> str | None:
    """Rotates through config.DESCRIPTION_APPEND_POOL (which includes None -
    "no line" - as a valid choice) using refresh_count as a pointer, so a
    listing cycles through all the options over time instead of ping-ponging
    between just two. Guaranteed to differ from last cycle's choice whenever
    the pool has more than one option."""
    pool = config.DESCRIPTION_APPEND_POOL
    if not pool:
        return None
    idx = listing.get("refresh_count", 0) % len(pool)
    choice = pool[idx]
    last = listing.get("last_description_variant")
    if choice == last and len(pool) > 1:
        choice = pool[(idx + 1) % len(pool)]
    return choice


def build_new_description(listing: dict, append_line: str | None) -> str:
    # Always rebuilt fresh from description_original - never appended to the
    # previous description_current - so there's no way for these lines to
    # pile up across refreshes, by construction.
    original = listing["description_original"]
    if not append_line:
        return original
    # Real eBay descriptions are HTML (even if simple, usually one or more
    # <p> tags) - appending raw "\n\n{line}" would land as bare text outside
    # any tag. Wrapping it as its own <p> keeps it rendering consistently
    # with the rest of the listing.
    return f"{original}\n<p>{append_line}</p>"


def compute_new_price(listing: dict) -> float:
    if not config.PRICE_ADJUSTMENT_ENABLED:
        return listing["price"]
    original_price = listing.get("original_price", listing["price"])
    floor = round(original_price * config.PRICE_FLOOR_MULTIPLIER, 2)
    proposed = round(listing["price"] * (1 - config.PRICE_ADJUSTMENT_PCT), 2)
    return max(proposed, floor)


def refresh_one(listing: dict, now: datetime, dry_run: bool) -> dict:
    variant = title_rules.generate_title(
        game_title=listing["game_title"],
        platform_key=listing["platform"],
        condition_key=listing["condition"],
        avoid_title=listing.get("current_title"),
        publisher=listing.get("publisher"),
        release_year=listing.get("release_year"),
    )
    append_line = pick_description_append(listing)
    new_description = build_new_description(listing, append_line)
    new_price = compute_new_price(listing)

    action = {
        "sku": listing["sku"],
        "old_title": listing.get("current_title"),
        "new_title": variant.title,
        "description_changed": new_description != listing["description_current"],
        "old_price": listing["price"],
        "new_price": new_price,
    }

    if dry_run:
        action["mode"] = "dry-run (no eBay call made)"
    else:
        token = ebay_client.get_access_token()
        result = ebay_client.end_and_relist(
            token, listing, variant.title, new_description, new_price
        )
        action["mode"] = "live"
        action["new_item_id"] = result.new_item_id
        listing["ebay_item_id"] = result.new_item_id

    listing["current_title"] = variant.title
    listing["last_title_variant_key"] = variant.variant_key
    listing["description_current"] = new_description
    listing["last_description_variant"] = append_line
    listing["price"] = new_price
    listing["last_refreshed_date"] = now.isoformat()
    listing["refresh_count"] = listing.get("refresh_count", 0) + 1
    listing.setdefault("refresh_history", []).append(
        {"date": now.isoformat(), "title": variant.title, "price": new_price}
    )
    return action


def run(db_path: str, dry_run: bool, write: bool) -> list[dict]:
    now = _now()
    db = load_db(db_path)
    selected = select_stale_listings(db, now)

    actions = []
    for listing in selected:
        try:
            actions.append(refresh_one(listing, now, dry_run))
        except Exception as e:
            # Stop the batch on the first real failure rather than silently
            # skipping items or leaving a listing half-updated (ended on eBay
            # but never relisted). Whatever succeeded before this item is
            # still recorded in `actions` and, if --write was passed, saved.
            actions.append({"sku": listing["sku"], "error": f"{type(e).__name__}: {e}"})
            break

    db.setdefault("config", {})["last_run"] = now.isoformat()
    if write:
        save_db(db_path, db)

    return actions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=config.LISTINGS_DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Persist changes to the db file. Combine with --dry-run to "
        "preview file changes without calling eBay.",
    )
    args = parser.parse_args()

    if not args.dry_run and not ebay_client.credentials_present():
        raise SystemExit(
            "No eBay credentials found in the environment and --dry-run was "
            "not passed. Refusing to run live without credentials. "
            "Use --dry-run to preview."
        )

    actions = run(args.db, dry_run=args.dry_run, write=args.write)

    print(f"Processed {len(actions)} listing(s):")
    for a in actions:
        print(f"  - {json.dumps(a, ensure_ascii=False)}")
    if not actions:
        print("  (nothing eligible this run)")


if __name__ == "__main__":
    main()
