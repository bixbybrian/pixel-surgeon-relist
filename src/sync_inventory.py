"""
Pulls live eBay inventory into data/listings.json's format.

Not yet wired to a full live bulk sync against a real account (needs
GetSellerList with pagination against the Trading API). What's here:

- parse_live_title(): best-effort parser that tries to split a real,
  human-written eBay title into (game_title, platform, condition), flagging
  anything it isn't confident about as needs_review rather than guessing
  wrong silently.
- run_demo(): runs that parser against a handful of real sample titles, so
  the parsing logic can be sanity-checked before pointing it at a live
  account.

Usage:
  python -m src.sync_inventory --demo
"""

from __future__ import annotations

import argparse
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
    "ps4": "PS4", "playstation 4": "PS4",
    "ps3": "PS3", "playstation 3": "PS3",
    "ps2": "PS2", "playstation 2": "PS2",
    "switch": "Switch", "nintendo switch": "Switch",
    "xbox 360": "Xbox360",
    "xbox": "Xbox",
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the title parser against local sample titles instead of a live account.",
    )
    args = parser.parse_args()

    if args.demo:
        run_demo()
    else:
        raise SystemExit(
            "Live bulk sync against a real eBay account isn't wired up yet "
            "(needs GetSellerList with pagination against the Trading API). "
            "Use --demo to sanity-check the title parser against sample titles."
        )


if __name__ == "__main__":
    main()
