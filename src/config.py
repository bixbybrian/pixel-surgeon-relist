"""Every tunable number/flag for the weekly relist sweep, in one place.
Edit values directly, or override via environment variables (useful for
GitHub Actions secrets/vars without touching code).
"""

from __future__ import annotations

import os

# How old (days since last_refreshed_date, falling back to first_listed_date)
# a listing must be before it's eligible for refresh.
AGE_THRESHOLD_DAYS = int(os.getenv("AGE_THRESHOLD_DAYS", "30"))

# Max number of listings refreshed in a single weekly run. Anything past this
# rolls into next week automatically (selection is always freshly computed).
WEEKLY_REFRESH_CAP = int(os.getenv("WEEKLY_REFRESH_CAP", "10"))

# Short, generic lines that may be appended below the untouched original
# description. None ("no line") is a valid, deliberate choice in the
# rotation - not just a fallback. Keep these true of ANY listing, not just
# games, and keep them short (1 sentence).
DESCRIPTION_APPEND_POOL: list[str | None] = [
    None,
    "Shipped with extra care and carefully packaged.",
    "Carefully packaged and shipped quickly.",
    "Fast, secure shipping on every order.",
]

# Price micro-adjustment: reduce price by PRICE_ADJUSTMENT_PCT on each
# refresh, but never below PRICE_FLOOR_MULTIPLIER * original_price - so a
# listing refreshed many times over months/years can't drift down forever.
PRICE_ADJUSTMENT_ENABLED = True
PRICE_ADJUSTMENT_PCT = float(os.getenv("PRICE_ADJUSTMENT_PCT", "0.01"))
PRICE_FLOOR_MULTIPLIER = float(os.getenv("PRICE_FLOOR_MULTIPLIER", "0.85"))

# Where the listings "database" lives.
LISTINGS_DB_PATH = os.getenv("LISTINGS_DB_PATH", "data/listings.json")
