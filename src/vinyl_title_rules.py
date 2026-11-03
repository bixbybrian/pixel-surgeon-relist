"""Title-variation engine for vinyl record listings.

Same locked-core philosophy as card_title_rules.py, plus one addition: a
separate, explicit CONDITION dimension (sealed vs. not), handled the same
way title_rules.py (the game engine) handles it - a rotating style choice,
never a locked fact baked into the core text. Brian confirmed this split
(2026-11-03): SEALED should behave like games' condition words, not like
cards' locked facts.

Each listing is reduced to:
- `prefix`: the artist/performer name, copied verbatim, ONLY when the real
  title actually leads with it as a clean, independently-readable phrase
  (e.g. "Herbie Hancock" in "Herbie Hancock Future Shock LP..."). Many real
  vinyl titles don't lead with the artist at all - soundtracks ("Goldfinger
  Soundtrack LP John Barry..."), Various Artists compilations ("Great
  Motion Picture Themes Volume 2 LP..."), or titles where splitting out a
  leading name would leave an awkward residual. In every one of those cases
  `prefix` is "" and the ENTIRE title (minus only the condition word, see
  below) is `core` - one single locked block, same fallback cards uses for
  its no-clean-prefix listings (e.g. the skateboard-sticker lot).
- `core`: everything else - album/soundtrack title, format (LP/7"/12"/2xLP/
  EP), label, catalogue number, year, and any other descriptor (Mono/
  Stereo, Gatefold, Picture Disc, Ltd/xx, Promo, "Original Pressing," etc.)
  - all locked verbatim, in original order, as one block. These are all
  facts a record buyer relies on, exactly the same reasoning as cards'
  count/completeness claims.
- `condition`: "sealed" or "used", extracted from whether the real title
  said SEALED (or an equivalent) - NOT part of the locked core text, and
  never invented in either direction.

What's allowed to vary:
- Whole-block ORDER (prefix-first / core-first) - only meaningful when
  `prefix` is non-empty.
- An optional FLAVOR WORD from config.VINYL_ADJECTIVE_POOL, placed before
  the leading block or after the trailing block.
- A CONDITION PHRASE, only when condition == "sealed" - placed independently
  of the flavor word, front or back. Never appears at all for a non-sealed
  record (same "no condition words on unconfirmed condition" rule games
  use). Deliberately does NOT include a bare "NEW" the way the game engine
  does: for a vintage original pressing, "NEW" risks reading as a new
  reissue rather than a sealed, decades-old original - a real buyer-facing
  distinction in record collecting that doesn't exist the same way for a
  shrink-wrapped modern game. See VINYL_SEALED_PHRASES below.
- Separator style between prefix and core (when both exist).
- Case style (normal / ALL CAPS).

Same rotation approach as card_title_rules.py: `refresh_count` picks a
start index into the full set of valid (<=80 char) candidates rather than
"first one that differs from the current title," so repeated refreshes
actually cycle through the real variety instead of oscillating between two
near-identical candidates once a title is close to the length limit.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config

EBAY_TITLE_MAX_CHARS = 80

ENABLE_ALL_CAPS_VARIANT = True

SEPARATORS = [" - ", " ", ": "]

# Only ever applied when condition == "sealed". Deliberately excludes a bare
# "NEW" - see module docstring on why that word is riskier for vintage
# vinyl than it is for games.
VINYL_SEALED_PHRASES = [
    "SEALED",
    "Sealed",
    "Still Sealed",
    "Factory Sealed",
    "Sealed Copy",
]


@dataclass
class VinylTitleVariant:
    title: str
    variant_key: str


def _case_styles() -> list[str]:
    return ["normal", "upper"] if ENABLE_ALL_CAPS_VARIANT else ["normal"]


def _apply_case(text: str, style: str) -> str:
    return text.upper() if style == "upper" else text


def generate_vinyl_title(
    prefix: str,
    core: str,
    condition: str,
    avoid_title: str | None = None,
    refresh_count: int = 0,
) -> VinylTitleVariant:
    """`prefix` and `core` are locked verbatim blocks - never split, reworded,
    or reordered internally. `condition` is "sealed" or "used"; a condition
    phrase is only ever added when condition == "sealed", and is never
    implied for anything else. `refresh_count` rotates a start index through
    the full set of valid (<=80 char) variants, same approach as
    card_title_rules.generate_card_title - see that module's docstring for
    why that beats "first candidate that differs from avoid_title."

    `prefix` may be "" for a listing with no clean leading artist/performer
    name to extract (soundtracks, Various Artists compilations, or titles
    where no split reads cleanly) - order/separator variation is then
    skipped (nothing to reorder against), and only the flavor word,
    condition phrase, and case still vary."""
    candidates: list[tuple[str, str]] = []

    if not prefix:
        orders: list[tuple[str, str, str]] = [("core_only", core, "")]
    else:
        orders = [
            ("prefix_first", prefix, core),
            ("core_first", core, prefix),
        ]

    condition_phrases: list[str | None]
    if condition == "sealed":
        condition_phrases = [None] + VINYL_SEALED_PHRASES
    else:
        # Never add a condition word for anything not confirmed sealed -
        # same rule title_rules.py uses for games.
        condition_phrases = [None]

    for order, lead, trail in orders:
        seps = [""] if order == "core_only" else SEPARATORS
        for separator in seps:
            base = f"{lead}{separator}{trail}"
            for flavor in config.VINYL_ADJECTIVE_POOL:
                flavor_positions = ["none"] if flavor is None else ["front", "back"]
                for flavor_pos in flavor_positions:
                    if flavor is None:
                        with_flavor = base
                    elif flavor_pos == "front":
                        with_flavor = f"{flavor} {base}"
                    else:
                        with_flavor = f"{base} {flavor}"

                    for cond_phrase in condition_phrases:
                        cond_positions = (
                            ["none"] if cond_phrase is None else ["front", "back"]
                        )
                        for cond_pos in cond_positions:
                            if cond_phrase is None:
                                body = with_flavor
                            elif cond_pos == "front":
                                body = f"{cond_phrase} {with_flavor}"
                            else:
                                body = f"{with_flavor} {cond_phrase}"

                            for case_style in _case_styles():
                                candidate_title = _apply_case(body, case_style)
                                variant_key = (
                                    f"{order}:{separator.strip() or 'nosep'}:"
                                    f"flavor={flavor or 'none'}:{flavor_pos}:"
                                    f"cond={cond_phrase or 'none'}:{cond_pos}:"
                                    f"{case_style}"
                                )
                                candidates.append((candidate_title, variant_key))

    valid = [
        (candidate_title, variant_key)
        for candidate_title, variant_key in candidates
        if len(candidate_title) <= EBAY_TITLE_MAX_CHARS
    ]

    if not valid:
        # Extreme fallback: the two locked blocks alone, truncated if necessary.
        fallback_text = f"{prefix} {core}".strip()
        return VinylTitleVariant(fallback_text[:EBAY_TITLE_MAX_CHARS], "fallback:locked-only")

    idx = refresh_count % len(valid)
    candidate_title, variant_key = valid[idx]
    if avoid_title is not None and candidate_title == avoid_title and len(valid) > 1:
        idx = (idx + 1) % len(valid)
        candidate_title, variant_key = valid[idx]
    return VinylTitleVariant(candidate_title, variant_key)
