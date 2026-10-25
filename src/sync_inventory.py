"""
Pulls live eBay inventory into data/listings.json's format.

What's here:

- parse_live_title(): best-effort parser that tries to split a real,
  human-written eBay title into (game_title, platform, condition), flagging
  anything it isn't confident about as needs_review rather than guessing
  wrong silently.
- run_demo(): runs that parser against a handful of real sample titles, so
  the parsing logic can be sanity-checked before pointing it at a live
  account.
- run_live(): the real sync - GetMyeBaySelling (via ebay_client) to find
  every active listing regardless of age, then GetItem per listing to pull
  its full description, then parse_live_title() to split each title into
  components. Existing entries (matched by ebay_item_id) keep their
  manually-set fields (publisher, release_year, refresh_count, refresh_history,
  last_description_variant) - only title/description/price/start_time are
  refreshed from eBay. New listings get fresh/blank defaults and are always
  flagged needs_review so a human looks at the parsed game_title/platform/
  condition before the tool ever touches them.

Usage:
  python -m src.sync_inventory --demo
  python -m src.sync_inventory --live --db data/listings.json          # preview only, no file changes
  python -m src.sync_inventory --live --db data/listings.json --write  # actually overwrite the file
"""

from __future__ import annotations

import argparse
import json
import re

# Only splits on a hyphen with whitespace on BOTH sides (the actual
# game-title/tail separator this tool's own title_rules.py uses) - never on
# a bare hyphen with no surrounding space, which shows up inside game
# titles themselves (e.g. "R-Type", "Spider-Man") and must not be treated
# as a separator.
TITLE_SPLIT_RE = re.compile(r"\s+-\s+")

_SEALED_WORDS = {
    "new", "sealed", "brand new", "brand new sealed", "new factory sealed",
    "factory sealed", "sealed brand new",
}

_PLATFORM_WORDS = {
    "playstation 5": "PS5", "ps5": "PS5",
    "playstation 4": "PS4", "ps4": "PS4",
    "playstation 3": "PS3", "ps3": "PS3",
    "playstation 2": "PS2", "ps2": "PS2",
    "playstation portable": "PSP", "psp": "PSP",
    "nintendo switch": "Switch", "switch": "Switch",
    "nintendo 3ds": "3DS",
    "nintendo ds": "DS",
    "game boy advance": "GBA", "gameboy advance": "GBA",
    "gamecube": "GameCube", "game cube": "GameCube",
    "nintendo 64": "N64",
    "xbox 360": "Xbox360",
    "xbox one": "XboxOne",
    "xbox series x": "XboxSeriesX", "xbox series s": "XboxSeriesS",
    "xbox": "Xbox",
    # Deliberately NOT included: bare "wii" / "ds" / "3ds" / "gba" / "n64" as
    # standalone 2-3 letter tokens - too easy to false-positive against
    # ordinary words ("cards", "words") or non-game Wii *accessories* (e.g.
    # "Nintendo Wii Remote Controller Sleeve" is not a Wii game). Every
    # phrase above requires the platform name spelled out or a brand prefix,
    # specifically so a plain accessory/manual/guide listing doesn't get
    # mistaken for a game. Add more specific phrases here if real inventory
    # turns up platforms this list is still missing - never a bare 2-3
    # letter abbreviation on its own.
}


def guess_condition(title_tail: str) -> tuple[str, bool]:
    """Returns (condition_key, confident). condition_key is 'sealed' or
    'used' - 'used' is the default/fallback when no sealed-indicating word
    is found, and confident=False flags that the guess should be reviewed
    rather than trusted outright for ambiguous titles."""
    lowered = title_tail.lower()
    for word in _SEALED_WORDS:
        if word in lowered:
            return "sealed", True
    return "used", True


def guess_platform(title_tail: str) -> tuple[str | None, bool]:
    """Returns (platform_key, confident). Longest/most-specific phrases are
    checked first so e.g. "xbox 360" doesn't get misread as "xbox"."""
    lowered = title_tail.lower()
    for phrase in sorted(_PLATFORM_WORDS, key=len, reverse=True):
        if phrase in lowered:
            return _PLATFORM_WORDS[phrase], True
    return None, False


def parse_live_title(title: str) -> dict:
    """Best-effort split of a real eBay title into components. Always
    returns a dict with game_title/platform/condition plus needs_review -
    never silently invents a platform it can't find."""
    parts = TITLE_SPLIT_RE.split(title, maxsplit=1)
    game_title = parts[0].strip()
    tail = parts[1] if len(parts) > 1 else ""

    condition_key, condition_confident = guess_condition(tail or title)
    platform_key, platform_confident = guess_platform(tail or title)

    needs_review = not platform_confident or not condition_confident

    return {
        "original_title": title,
        "game_title": game_title,
        "platform": platform_key,
        "condition": condition_key,
        "needs_review": needs_review,
    }


_DEMO_TITLES = [
    "Deathsmiles I & II - Sony PlayStation 4 PS4 New Sealed",
    "Akai Katana Shin - PlayStation 4 PS4 New Sealed",
    "Frogun Deluxe Edition - Nintendo Switch New Sealed",
    "TriggerHeart Exelica - Nintendo Switch",
    "R-Type Final 2 - Sony PlayStation 4 PS4",
    "The Evil Within - Sony PlayStation 3",
    "Chernobylite - Sony PlayStation 4 New Sealed PS4",
    "Might And Magic: Clash Of Heroes Definitive Edition - Sony PlayStation 4",
    "Pressure Overdrive - Sony PlayStation 4",
    "Indiana Jones and the Emperor's Tomb - Xbox",
    "Battlefield 2 - PlayStation 2",
    "Blinx: The Time Sweeper - Xbox",
]


def run_demo() -> None:
    for title in _DEMO_TITLES:
        parsed = parse_live_title(title)
        flag = "  <-- needs_review" if parsed["needs_review"] else ""
        print(
            f"{title!r}\n"
            f"  -> game_title={parsed['game_title']!r} "
            f"platform={parsed['platform']!r} condition={parsed['condition']!r}{flag}"
        )


def _sku_for(item_id: str, existing_by_item_id: dict) -> str:
    if item_id in existing_by_item_id:
        return existing_by_item_id[item_id]["sku"]
    return f"EBAY-{item_id}"


def run_live(db_path: str, write: bool) -> None:
    """Fetches every real active listing from eBay and builds/updates
    data/listings.json from it. Always prints a full preview; only writes
    the file if write=True, so this is safe to run first without --write to
    sanity-check what was found."""
    from . import ebay_client  # local import - keeps --demo usable with no eBay creds at all

    if not ebay_client.credentials_present():
        raise SystemExit(
            "No eBay credentials found in the environment (EBAY_CLIENT_ID / "
            "EBAY_CLIENT_SECRET / EBAY_REFRESH_TOKEN). See README.md before "
            "running --live."
        )

    try:
        with open(db_path, "r", encoding="utf-8") as f:
            existing_db = json.load(f)
    except FileNotFoundError:
        existing_db = {"config": {}, "listings": []}

    existing_by_item_id = {
        listing["ebay_item_id"]: listing for listing in existing_db.get("listings", [])
    }

    print(f"Fetching access token ({ebay_client.EBAY_ENV})...")
    token = ebay_client.get_access_token()

    print("Fetching active listings (GetMyeBaySelling)...")
    active = ebay_client.get_active_listings(token)
    print(f"Found {len(active)} active listing(s). Pulling full details for each...\n")

    new_listings = []
    for i, light in enumerate(active, start=1):
        item_id = light["item_id"]
        details = ebay_client.get_item(token, item_id)
        parsed = parse_live_title(details.title)

        prior = existing_by_item_id.get(item_id)

        entry = {
            "sku": _sku_for(item_id, existing_by_item_id),
            "ebay_item_id": item_id,
            "game_title": parsed["game_title"],
            "platform": parsed["platform"],
            "condition": parsed["condition"],
            "current_title": details.title,
            "description_original": (
                prior["description_original"] if prior else (details.description or "")
            ),
            "description_current": details.description or (prior or {}).get(
                "description_current", ""
            ),
            "price": details.price if details.price is not None else light.get("price"),
            "first_listed_date": details.start_time or (prior or {}).get(
                "first_listed_date"
            ),
            "last_refreshed_date": (prior or {}).get("last_refreshed_date"),
            "refresh_count": (prior or {}).get("refresh_count", 0),
            "last_title_variant_key": (prior or {}).get("last_title_variant_key"),
            "refresh_history": (prior or {}).get("refresh_history", []),
            "publisher": (prior or {}).get("publisher"),
            "release_year": (prior or {}).get("release_year"),
            "original_price": (prior or {}).get(
                "original_price", details.price if details.price is not None else light.get("price")
            ),
            "last_description_variant": (prior or {}).get("last_description_variant"),
            # Purely a parse-confidence flag (platform/condition couldn't be
            # determined) - NOT "this is a brand-new sync". relist_worker
            # treats needs_review=True as permanently out of scope for the
            # automated sweep until a human clears it by hand.
            "needs_review": parsed["needs_review"],
        }
        new_listings.append(entry)

        flag = "  <-- NEEDS REVIEW" if entry["needs_review"] else ""
        print(
            f"[{i}/{len(active)}] {item_id}  {details.title!r}\n"
            f"    -> game_title={entry['game_title']!r} platform={entry['platform']!r} "
            f"condition={entry['condition']!r} price={entry['price']}{flag}"
        )

    existing_db["listings"] = new_listings
    existing_db.setdefault("config", {})
    existing_db["config"].setdefault("age_threshold_days", 30)
    existing_db["config"].setdefault("weekly_refresh_cap", 10)

    review_count = sum(1 for l in new_listings if l["needs_review"])
    print(f"\n{len(new_listings)} listing(s) total, {review_count} flagged needs_review.")

    if write:
        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(existing_db, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"Wrote {db_path}.")
    else:
        print("Preview only - rerun with --write to save this to the database file.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the title parser against local sample titles instead of a live account.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Fetch real active listings from eBay (requires credentials in the environment).",
    )
    parser.add_argument("--db", default="data/listings.json")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Persist the live sync result to --db. Without this, --live only previews.",
    )
    args = parser.parse_args()

    if args.demo:
        run_demo()
    elif args.live:
        run_live(args.db, write=args.write)
    else:
        raise SystemExit("Pass --demo or --live. See --help.")


if __name__ == "__main__":
    main()
