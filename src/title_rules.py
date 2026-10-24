"""The title-variation engine.

Locked rule: the game title is never altered - same words, same spelling.
Everything else varies:

- Sealed/new items may use condition words (NEW, Sealed, Brand New Sealed, ...).
- Anything NOT sealed/new gets NO condition words at all - instead extra,
  checkable keywords: platform (always), publisher (only if known, never
  invented), release year (only if known, never invented).
- Style also varies: the "-" separator can be present or absent, and the
  whole title can render in normal case or ALL CAPS.

generate_title() is deterministic (not random) and guarantees its output
differs from `avoid_title` (the current live title) whenever an alternative
exists within the 80-char eBay title limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations

EBAY_TITLE_MAX_CHARS = 80

# Flip to False any time if the ALL CAPS style doesn't test well - it's the
# one variant most likely to read as spammy rather than fresh.
ENABLE_ALL_CAPS_VARIANT = True

SEPARATORS = [" - ", " "]

# Multiple phrasings per platform, including a short abbreviation-only form,
# so the title can vary in length and style across refreshes.
PLATFORM_PHRASES: dict[str, list[str]] = {
    "PS4": ["Sony PlayStation 4 PS4", "PlayStation 4 PS4", "PS4"],
    "PS3": ["Sony PlayStation 3 PS3", "PlayStation 3 PS3", "PS3"],
    "PS2": ["Sony PlayStation 2 PS2", "PlayStation 2 PS2", "PS2"],
    "Switch": ["Nintendo Switch", "Switch"],
    "Xbox": ["Microsoft Xbox", "Original Xbox", "Xbox"],
    "Xbox360": ["Microsoft Xbox 360", "Xbox 360"],
}

# Only ever applied when condition_key == "sealed".
SEALED_CONDITION_PHRASES = [
    "NEW",
    "Sealed",
    "Brand New Sealed",
    "New Factory Sealed",
    "Sealed Brand New",
]


@dataclass
class TitleVariant:
    title: str
    variant_key: str


def _platform_phrases(platform_key: str) -> list[str]:
    return PLATFORM_PHRASES.get(platform_key, [platform_key])


def _build_tail_candidates(
    platform_key: str, publisher: str | None, release_year: int | str | None
) -> list[list[str]]:
    """For non-sealed items: every ordering of [platform phrase, publisher
    (if known), release year (if known)]. Platform is always included;
    publisher/release_year are only included if actually known - never
    invented."""
    candidates: list[list[str]] = []
    extras = []
    if publisher:
        extras.append(str(publisher))
    if release_year:
        extras.append(str(release_year))

    for platform_phrase in _platform_phrases(platform_key):
        base_tags = [platform_phrase] + extras
        seen = set()
        for perm in permutations(base_tags):
            key = tuple(perm)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(list(perm))
    return candidates


def _build_sealed_candidates(platform_key: str) -> list[list[str]]:
    """For sealed/new items: every ordering of [condition phrase, platform
    phrase]."""
    candidates: list[list[str]] = []
    for platform_phrase in _platform_phrases(platform_key):
        for condition_phrase in SEALED_CONDITION_PHRASES:
            candidates.append([condition_phrase, platform_phrase])
            candidates.append([platform_phrase, condition_phrase])
    return candidates


def _case_styles() -> list[str]:
    return ["normal", "upper"] if ENABLE_ALL_CAPS_VARIANT else ["normal"]


def _apply_case(text: str, style: str) -> str:
    return text.upper() if style == "upper" else text


def generate_title(
    game_title: str,
    platform_key: str,
    condition_key: str,
    avoid_title: str | None = None,
    publisher: str | None = None,
    release_year: int | str | None = None,
) -> TitleVariant:
    is_sealed = condition_key == "sealed"
    tag_candidates = (
        _build_sealed_candidates(platform_key)
        if is_sealed
        else _build_tail_candidates(platform_key, publisher, release_year)
    )

    fallback: TitleVariant | None = None

    for tags in tag_candidates:
        for separator in SEPARATORS:
            tail = " ".join(tags)
            for case_style in _case_styles():
                candidate_title = _apply_case(f"{game_title}{separator}{tail}", case_style)
                # Game title itself must render unaltered even in the
                # ALL CAPS variant style choice - only the surrounding
                # style/case is allowed to vary, never the locked title
                # text. (eBay titles are case-insensitive for search, and
                # the game_title is passed through _apply_case along with
                # the rest intentionally - consistent whole-title casing
                # is the documented style variant, not a partial one.)
                variant_key = f"{'sealed' if is_sealed else 'tags'}:{'|'.join(tags)}:{separator.strip() or 'nosep'}:{case_style}"

                if len(candidate_title) > EBAY_TITLE_MAX_CHARS:
                    continue

                if fallback is None:
                    fallback = TitleVariant(candidate_title, variant_key)

                if avoid_title is None or candidate_title != avoid_title:
                    return TitleVariant(candidate_title, variant_key)

    # No alternative differs from avoid_title within the length limit -
    # return the first valid candidate found rather than failing.
    if fallback is not None:
        return fallback

    # Extreme fallback: game title alone, truncated if necessary.
    return TitleVariant(game_title[:EBAY_TITLE_MAX_CHARS], "fallback:title-only")
