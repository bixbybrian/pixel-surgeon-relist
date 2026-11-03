"""Title-variation engine for trading-card listings (sets, lots, singles).

Design principle, per Brian (2026-11-02): card titles make specific factual
claims a buyer relies on - card counts, set/manufacturer names, completeness
claims ("Complete Set", "Near Complete", "Lot of 53", "About 100 Mixed
Duplicates"). Those are NEVER invented, reworded, or reordered - only
adjectives and simple whole-block ordering can vary. This is a stricter
locked/variable split than games, where only the game title itself was
locked and everything else (platform phrasing, condition words) was free.

Each listing is reduced to exactly two locked blocks, both copied verbatim
from the listing's own real title - nothing here ever invents or rewrites
either block's text:

- `prefix`: year + manufacturer, e.g. "1980 Topps" - always exactly this,
  nothing more (no franchise name, no "Series 1", no count).
- `core`: everything else from the original title, as ONE locked block,
  internally untouched - e.g. "Empire Strikes Back Near Complete Set 329
  Cards Stickers in Binder". This deliberately keeps franchise name, series/
  set qualifiers, completeness claims, and counts all together and in their
  original relative order, since splitting them further risks an automated
  reordering that changes what a count/completeness claim appears to modify.

What's allowed to vary between refreshes:
- Whole-block ORDER: prefix-first or core-first (this is the "simple
  sentence ordering" Brian asked for - swapping which locked block leads,
  never reordering text *within* a block).
- An optional FLAVOR WORD from config.CARD_ADJECTIVE_POOL, placed before the
  leading block or after the trailing block - never inserted inside either
  locked block. None ("no flavor word") is a valid, deliberate rotation
  choice, same principle as DESCRIPTION_APPEND_POOL.
- Separator style between the two blocks.
- Case style (normal / ALL CAPS) - Brian confirmed playfulness is fine for
  card listings.

Many real card titles are already near eBay's 80-char limit (sellers pack
these with search keywords), so adding a flavor word isn't always possible -
over-length candidates are skipped, same length-guard pattern as
title_rules.py (the game engine), falling back to whichever variant fits.

Rotation, not "first that differs": a lot of real listings here only have
room for 2-3 valid candidates once the 80-char limit prunes the rest (see
above), and a naive "return the first candidate that isn't the current
title" always lands on the same couple of candidates in the same order -
in practice that means oscillating forever between just a case-flip of one
single order/separator choice, never actually reaching core-first ordering
or a flavor word, even though both are valid and in the candidate list.
Instead, `refresh_count` picks a rotating START index into the full, valid
candidate list (same philosophy as relist_worker.pick_description_append's
`refresh_count % len(pool)`), so repeated refreshes actually cycle through
the real variety - order, separator, flavor word, flavor position, case -
rather than bouncing between two. `avoid_title` is kept as a one-step safety
net only, for the rare case the rotated pick happens to match it exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config

EBAY_TITLE_MAX_CHARS = 80

# Brian confirmed playfulness/ALL CAPS is fine for card listings too.
ENABLE_ALL_CAPS_VARIANT = True

SEPARATORS = [" - ", " ", ": "]


@dataclass
class CardTitleVariant:
    title: str
    variant_key: str


def _case_styles() -> list[str]:
    return ["normal", "upper"] if ENABLE_ALL_CAPS_VARIANT else ["normal"]


def _apply_case(text: str, style: str) -> str:
    return text.upper() if style == "upper" else text


def generate_card_title(
    prefix: str,
    core: str,
    avoid_title: str | None = None,
    refresh_count: int = 0,
) -> CardTitleVariant:
    """`prefix` and `core` are both locked verbatim blocks - this function
    never splits, rewords, or reorders text within either one. It only
    varies which block leads, an optional flavor word, the separator, and
    case style. Deterministic (not random); `refresh_count` rotates through
    the full set of valid (fits-in-80-chars) variants over successive calls
    rather than always returning the first one that differs from
    `avoid_title` - see module docstring for why that distinction matters.

    `prefix` may be "" for a listing with no clean year/manufacturer token
    to extract (e.g. a multi-decade mixed lot) - order/separator variation
    is then skipped entirely (nothing to reorder against), and only the
    flavor word and case style still vary."""
    candidates: list[tuple[str, str]] = []

    if not prefix:
        orders: list[tuple[str, str, str]] = [("core_only", core, "")]
    else:
        orders = [
            ("prefix_first", prefix, core),
            ("core_first", core, prefix),
        ]

    for order, lead, trail in orders:
        seps = [""] if order == "core_only" else SEPARATORS
        for separator in seps:
            base = f"{lead}{separator}{trail}"
            for flavor in config.CARD_ADJECTIVE_POOL:
                flavor_positions = ["none"] if flavor is None else ["front", "back"]
                for flavor_pos in flavor_positions:
                    if flavor is None:
                        body = base
                    elif flavor_pos == "front":
                        body = f"{flavor} {base}"
                    else:
                        body = f"{base} {flavor}"

                    for case_style in _case_styles():
                        candidate_title = _apply_case(body, case_style)
                        variant_key = (
                            f"{order}:{separator.strip() or 'nosep'}:"
                            f"flavor={flavor or 'none'}:{flavor_pos}:{case_style}"
                        )
                        candidates.append((candidate_title, variant_key))

    valid = [
        (candidate_title, variant_key)
        for candidate_title, variant_key in candidates
        if len(candidate_title) <= EBAY_TITLE_MAX_CHARS
    ]

    if not valid:
        # Extreme fallback: the two locked blocks alone, truncated if necessary.
        return CardTitleVariant(f"{prefix} {core}"[:EBAY_TITLE_MAX_CHARS], "fallback:locked-only")

    idx = refresh_count % len(valid)
    candidate_title, variant_key = valid[idx]
    if avoid_title is not None and candidate_title == avoid_title and len(valid) > 1:
        idx = (idx + 1) % len(valid)
        candidate_title, variant_key = valid[idx]
    return CardTitleVariant(candidate_title, variant_key)
