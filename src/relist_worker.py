"""
Weekday sweep across ALL categories: finds listings older than
AGE_THRESHOLD_DAYS in data/listings.json (games), data/cards.json (trading
cards), and data/vinyl.json (vinyl records), refreshes up to
REFRESH_CAP_PER_RUN of them from one unified oldest-first pool, and writes
each touched database back to its own file.

Unified pool, not per-category caps: a record listed in 2026-09 and a game
listed in 2026-10 are compared on the same "how stale is this" axis and
compete for the same shared slots. This is deliberate - the whole point of
REFRESH_CAP_PER_RUN / the weekday cron cadence (see config.py) is one small,
steady trickle across the whole shop, not an extra trickle bolted on per
category.

Category dispatch, per listing:
  - listing["category"] == "trading_card" -> card_title_rules.generate_card_title()
    (locked prefix/core blocks; see that module's docstring)
  - listing["category"] == "vinyl_record" -> vinyl_title_rules.generate_vinyl_title()
    (locked prefix/core blocks PLUS a separate sealed/used condition
    dimension; see that module's docstring)
  - anything else (games predate the "category" field and simply don't have
    one - absence of the field means "game", by historical convention) ->
    title_rules.generate_title() (locked game_title; platform/condition vary)

Run modes:
  --dry-run   Always safe. Prints exactly what would change; never calls
              eBay; still updates the db files' "history" only if you also
              pass --write (so you can preview without touching any file
              at all).
  (default)   Requires real eBay credentials in the environment. Calls
              ebay_client.end_and_relist for real.

Usage:
  python -m src.relist_worker --dry-run
  python -m src.relist_worker --dry-run --write
  python -m src.relist_worker --dry-run --db path/to/games.json \
      --cards-db path/to/cards.json --vinyl-db path/to/vinyl.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from . import card_title_rules, config, ebay_client, title_rules, vinyl_title_rules

UTC = timezone.utc

CATEGORY_GAME = "game"
CATEGORY_CARD = "trading_card"
CATEGORY_VINYL = "vinyl_record"


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


def _category_of(listing: dict) -> str:
    # Games predate the "category" field entirely - no key at all means
    # "game", not a data error. Cards and vinyl always carry their category
    # explicitly (set when their database files were built).
    return listing.get("category") or CATEGORY_GAME


def select_stale_listings(dbs: dict[str, dict], now: datetime) -> list[tuple[str, dict]]:
    """`dbs` maps a source key (e.g. "games"/"cards"/"vinyl") to its
    already-loaded db dict. Returns a single oldest-first list of
    (source_key, listing) pairs, pooled across every source and capped
    ONCE at config.REFRESH_CAP_PER_RUN - see module docstring on why this
    is one shared pool rather than a cap per category."""
    threshold = timedelta(days=config.AGE_THRESHOLD_DAYS)
    eligible: list[tuple[timedelta, str, dict]] = []
    for source_key, db in dbs.items():
        for listing in db["listings"]:
            # needs_review=True means a human hasn't cleared this listing
            # for the automated sweep yet (sync_inventory.py couldn't
            # confidently parse a game platform, or - for cards/vinyl -
            # every entry starts this way pending Brian's review of the
            # title methodology). Permanently out of scope until cleared by
            # hand in the database file - never auto-cleared on a guess,
            # same gate for every category.
            if listing.get("needs_review"):
                continue
            anchor = listing.get("last_refreshed_date") or listing["first_listed_date"]
            age = now - _parse_dt(anchor)
            if age >= threshold:
                eligible.append((age, source_key, listing))
    # Oldest (largest age) first, across the whole pool.
    eligible.sort(key=lambda triple: triple[0], reverse=True)
    return [
        (source_key, listing)
        for _age, source_key, listing in eligible[: config.REFRESH_CAP_PER_RUN]
    ]


def pick_description_append(listing: dict) -> str | None:
    """Rotates through config.DESCRIPTION_APPEND_POOL (which includes None -
    "no line" - as a valid choice) using refresh_count as a pointer, so a
    listing cycles through all the options over time instead of ping-ponging
    between just two. Guaranteed to differ from last cycle's choice whenever
    the pool has more than one option. Same logic for every category - the
    pool is deliberately generic enough to be true of any listing."""
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


def _generate_new_title(listing: dict):
    """Dispatches to the right title-variation engine for this listing's
    category. Returns a TitleVariant/CardTitleVariant/VinylTitleVariant -
    all three are duck-typed the same way (`.title`, `.variant_key`), so
    refresh_one() below doesn't need to care which one it got."""
    category = _category_of(listing)
    if category == CATEGORY_CARD:
        return card_title_rules.generate_card_title(
            prefix=listing["prefix"],
            core=listing["core"],
            avoid_title=listing.get("current_title"),
            refresh_count=listing.get("refresh_count", 0),
        )
    if category == CATEGORY_VINYL:
        return vinyl_title_rules.generate_vinyl_title(
            prefix=listing["prefix"],
            core=listing["core"],
            condition=listing["condition"],
            avoid_title=listing.get("current_title"),
            refresh_count=listing.get("refresh_count", 0),
        )
    return title_rules.generate_title(
        game_title=listing["game_title"],
        platform_key=listing["platform"],
        condition_key=listing["condition"],
        avoid_title=listing.get("current_title"),
        publisher=listing.get("publisher"),
        release_year=listing.get("release_year"),
    )


def refresh_one(listing: dict, now: datetime, dry_run: bool) -> dict:
    variant = _generate_new_title(listing)
    append_line = pick_description_append(listing)
    new_description = build_new_description(listing, append_line)
    new_price = compute_new_price(listing)

    action = {
        "sku": listing["sku"],
        "category": _category_of(listing),
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


def run(
    listings_path: str, cards_path: str, vinyl_path: str, dry_run: bool, write: bool
) -> list[dict]:
    now = _now()
    dbs = {
        "games": load_db(listings_path),
        "cards": load_db(cards_path),
        "vinyl": load_db(vinyl_path),
    }
    selected = select_stale_listings(dbs, now)

    actions = []
    for source_key, listing in selected:
        try:
            actions.append(refresh_one(listing, now, dry_run))
        except Exception as e:
            # A failed attempt must never leave the listing untouched - if it
            # does, select_stale_listings() just picks the exact same "oldest"
            # listing again on the very next run, forever. That's not
            # hypothetical: a sold/ended PS3 listing (EBAY-394362394218,
            # "The Evil Within") sat at the front of the oldest-first queue
            # and failed EndFixedPriceItem with "The auction has been closed"
            # every single weekday from 2026-10-27 through (at least)
            # 2026-11-04 - nine straight "successful" GitHub Actions runs that
            # silently refreshed nothing at all, games/cards/vinyl included,
            # because this one stale record was permanently first in line.
            #
            # Fix: flag the failing listing needs_review (same gate a human
            # already has to clear elsewhere) and record the error on it, so
            # it's excluded from every future sweep until someone looks at
            # it - then keep going rather than aborting the whole run, so one
            # bad listing can never again block everything behind it.
            err_msg = f"{type(e).__name__}: {e}"
            actions.append({"sku": listing["sku"], "error": err_msg})
            listing["needs_review"] = True
            listing["last_error"] = {"date": now.isoformat(), "error": err_msg}
            continue

    if write:
        paths = {"games": listings_path, "cards": cards_path, "vinyl": vinyl_path}
        for source_key, db in dbs.items():
            db.setdefault("config", {})["last_run"] = now.isoformat()
            save_db(paths[source_key], db)

    return actions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=config.LISTINGS_DB_PATH, help="Games database path."
    )
    parser.add_argument(
        "--cards-db", default=config.CARDS_DB_PATH, help="Trading-card database path."
    )
    parser.add_argument(
        "--vinyl-db", default=config.VINYL_DB_PATH, help="Vinyl-record database path."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Persist changes to all db files. Combine with --dry-run to "
        "preview file changes without calling eBay.",
    )
    args = parser.parse_args()

    if not args.dry_run and not ebay_client.credentials_present():
        raise SystemExit(
            "No eBay credentials found in the environment and --dry-run was "
            "not passed. Refusing to run live without credentials. "
            "Use --dry-run to preview."
        )

    actions = run(
        args.db, args.cards_db, args.vinyl_db, dry_run=args.dry_run, write=args.write
    )

    print(f"Processed {len(actions)} listing(s):")
    for a in actions:
        print(f"  - {json.dumps(a, ensure_ascii=False)}")
    if not actions:
        print("  (nothing eligible this run)")


if __name__ == "__main__":
    main()
