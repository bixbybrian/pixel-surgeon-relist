# Pixel Surgeon Games — Weekly Listing Refresh

Automatically finds eBay listings older than 30 days, ends them, and reposts
a fresh listing with a genuinely different title (game title never touched),
a small rotating description line, and a 1% price micro-adjustment. Runs
weekly via GitHub Actions, at zero hosting cost, capped at 10 listings per
run so it doesn't try to touch the whole inventory in one pass.

## Read this before going live: more encouraging than it first looked

Earlier research flagged that eBay's own Search team publicly called frequent
end-and-relist a "myth" that resets performance data. A colleague's pushback
on that framing turned out to have real technical merit, confirmed directly
against eBay's Trading API docs:

- eBay ties listing continuity to the **specific API call**, not to "ending
  and reposting" as a blanket concept. `RelistItem`/`RelistFixedPriceItem`
  (using the original Item ID) is documented as different from `AddItem`
  (a genuinely brand-new listing).
- eBay's docs state plainly: **"If the item was being watched when the
  listing ended, it will continue to be watched when the item is
  relisted."** — confirmed via `RelistItem`.
- eBay's docs also state the **performance score carries over** on relist,
  "in most cases" — and, reading the fuller passage, this appears to chain
  across repeated relists (each relist passes its score to the next), not
  just a one-time inheritance that dies after the first cycle.

**This is what `ebay_client.py` already does** — it calls
`EndFixedPriceItem` + `RelistFixedPriceItem` with the original Item ID, not
`AddItem`. So the tool was already built on the continuity-preserving path,
not the "start from zero" path.

Two things keep this from being fully resolved, though:

1. **Relisting still assigns a new Item ID** — meaning a new URL either way,
   `RelistItem` or `AddItem`. So the *Google-indexing* half of the original
   concern isn't really settled by which API call is used.
2. **Whether changing the title on relist affects watcher/score
   continuity isn't explicitly documented either way.** The "watchers carry
   over" language isn't conditioned on the title staying the same, so the
   natural reading is that it doesn't matter — but that's an inference, not
   a quoted guarantee. Sandbox testing won't settle this either, since a
   sandbox account has no real watchers or performance history to observe —
   this is genuinely only verifiable against production data.

Practical read: the risk picture is better than it first looked, since the
tool already avoids the riskier `AddItem` path. Still worth watching actual
view/impression data on a handful of real listings once this goes live in
production, specifically because point 2 above can't be checked any other
way.

## Current status: developer account approved, moving to Sandbox testing

Everything runs end-to-end in **dry-run mode** against the seed data in
`data/listings.json`, and `src/ebay_client.py` is implemented against the
specific eBay Trading API calls confirmed via eBay's own docs (not yet
tested against any real — even Sandbox — account):

- `GetItem` → `Item/ListingDetails/StartTime` for real listing age.
- `EndFixedPriceItem` to end the active listing, then `RelistFixedPriceItem`
  (within 90 days) to relist it fresh with a new ItemID and the new
  title/description/price all set in that same call.

Two details remain genuinely unconfirmed — not guessable from docs, need a
real test call:

1. **OAuth scope** for these Trading API calls under the `X-EBAY-API-IAF-TOKEN`
   header — eBay's per-method pages don't list one. Defaulted to the base
   `.../oauth/api_scope`; widen if Sandbox returns Unauthorized.
2. **`EndingReason` value** — none of eBay's documented options
   (`NotAvailable`, `Incorrect`, `LostOrBroken`, `OtherListingError`, ...)
   describe "ending on purpose to relist." Defaulted to `NotAvailable` as the
   closest fit; may need adjusting based on what a real test call says.

`ebay_client.py` defaults to eBay's **Sandbox** environment (`EBAY_ENV=sandbox`)
so a misconfigured first run can't touch real listings — switch to
`EBAY_ENV=production` only once a Sandbox test has actually succeeded. The
GitHub Actions workflow still runs with `--dry-run` regardless.

## eBay Developer setup

1. ✅ Register at [developer.ebay.com](https://developer.ebay.com/) (free,
   separate account from your eBay seller login). **Approved** — developer
   username `blueprocess`.
2. **In progress:** under **My Account → Application Keys**, create a
   **Sandbox** keyset first (a fake store to test against safely), and a
   **Production** keyset only once Sandbox testing has actually succeeded.
3. **Redirect URL (RuName):** on the Sandbox App ID's **User Tokens** page,
   under "Get a Token from eBay via Your Application," set up a Redirect URL
   with a Display Title, Privacy Policy URL, Auth Accepted URL, and Auth
   Declined URL. This repo's `docs/` folder holds ready-made pages for all
   three — once GitHub Pages is enabled for this repo (Settings → Pages →
   source: `main` branch, `/docs` folder), use:
   - Privacy Policy: `docs/privacy-policy.html`
   - Auth Accepted: `docs/oauth-accepted.html`
   - Auth Declined: `docs/oauth-declined.html`
4. Generate a **user OAuth refresh token**: on that same User Tokens page,
   run the sign-in flow as a Sandbox test seller account, approve access,
   and exchange the resulting authorization code for a refresh token
   (eBay's docs walk through the exact token-exchange request).
5. Add repository secrets in GitHub (Settings → Secrets and variables →
   Actions): `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_REFRESH_TOKEN`, and
   `EBAY_ENV` (`sandbox` or `production`).

Once you have Sandbox credentials, run a single item through
`ebay_client.end_and_relist()` by hand (not via the Action yet) to settle
the two unconfirmed details above before trusting the weekly cron with it.

## How the pieces fit together

```
data/listings.json          the "database" — one row per listing, committed
                             to the repo and updated by every run
src/config.py                every tunable number/flag in one place
src/title_rules.py           the title-variation engine (see below)
src/ebay_client.py           OAuth + eBay Trading API calls
src/sync_inventory.py        pulls live eBay inventory into listings.json
src/relist_worker.py         the weekly sweep — this is what the cron runs
.github/workflows/relist.yml the weekly schedule (Mondays, GitHub Actions)
docs/                        GitHub Pages site (privacy policy + OAuth
                              redirect pages needed for the Redirect URL/RuName)
```

### The listings database (`data/listings.json`)

One entry per SKU. Key fields:

- `game_title` / `platform` / `condition` — the parsed, locked components
  used to build titles. `game_title` is never altered by the tool.
- `publisher` / `release_year` — optional, only used if known (never
  invented). Used as extra title keywords for non-sealed items.
- `current_title` / `description_current` — what's live right now (as far as
  this file knows).
- `description_original` — the description text as originally written.
  **Never edited or overwritten** — only ever read from, to build
  `description_current` fresh each time.
- `price` / `original_price` — `price` is the current live price;
  `original_price` is set once (first sync) and never changes — it's the
  anchor the price-floor safeguard measures against (see below).
- `last_refreshed_date` — what "age" is measured against. Falls back to
  `first_listed_date` if never refreshed.
- `last_description_variant` — which description line (if any) was used last
  time, so the next refresh picks a different one.
- `refresh_count` / `refresh_history` — a running log.

The seed data is **demo data**: 12 entries built from sample titles you'd
shared, with made-up `first_listed_date` values. `publisher`/`release_year`
are left `null` throughout — replace with real data (eBay listings often
carry Publisher as an item specific) once syncing against your actual
account.

### Weekly sweep logic (`src/relist_worker.py`)

1. For every listing, compute days since `last_refreshed_date`.
2. Keep only listings ≥ `AGE_THRESHOLD_DAYS` (default **30**).
3. Sort oldest-first, take the first `WEEKLY_REFRESH_CAP` (default **10**).
4. For each: generate a new title, rebuild the description, compute the new
   price, then either print what would happen (`--dry-run`) or call eBay.
5. Anything past the cap rolls into next week automatically (sort is always
   freshly computed from `last_refreshed_date`).

### Title-variation rules (`src/title_rules.py`)

Locked rule: the game title is never altered — same words, same spelling.
Everything else varies, per your spec:

- **Sealed/new items** may use condition words: `NEW`, `Sealed`,
  `Brand New Sealed`, etc.
- **Anything not sealed/new gets no condition words at all.** Instead it
  leans on extra, checkable keywords: platform (always), publisher (only if
  known), release year (only if known) — never invented.
- **Style also varies:** the `-` separator can be present or absent, and the
  whole title can render in normal case or ALL CAPS (`ENABLE_ALL_CAPS_VARIANT`
  in `title_rules.py` — flip to `False` any time if that style doesn't test
  well; it's the one variant most likely to look spammy rather than fresh).

Still worth a read-through: the exact phrase pools (`PLATFORM_PHRASES`,
`SEALED_CONDITION_PHRASES` at the top of `title_rules.py`) — edit freely.

### Description handling (`src/relist_worker.py` + `config.DESCRIPTION_APPEND_POOL`)

Original description text is never edited or trimmed. On each refresh, the
tool rebuilds the description fresh from `description_original` and may
append **one** short, generic line — generic enough to be true of *any*
listing, not just games — rotating through a pool that includes "no line"
as a valid choice, so repeated refreshes never pile up multiple lines (each
refresh is rebuilt from the untouched original, not appended-to-the-previous).
Edit the pool in `config.py` freely.

### Price micro-adjustment (`config.py` + `src/relist_worker.py`)

Each refresh drops the price by **1%** (`PRICE_ADJUSTMENT_PCT`), floored at
**85% of the original first-listed price** (`PRICE_FLOOR_MULTIPLIER`) so a
listing refreshed many times over months/years can't drift down
indefinitely — once that floor is hit, price just stops changing on future
refreshes. Both numbers are adjustable in `config.py`.

## Running it yourself

```bash
pip install -r requirements.txt

# Preview this week's sweep against the seed data (no file changes, no eBay calls):
python -m src.relist_worker --dry-run

# Same, but also write the resulting last_refreshed_date/history to the file:
python -m src.relist_worker --dry-run --write

# Preview how live eBay titles would get parsed into game_title/platform/condition:
python -m src.sync_inventory --demo
```

## Open questions before this goes live

1. **Test the actual premise first** — see the caution at the top. Worth
   watching real view/impression data on a few refreshed listings before
   trusting the full weekly cron.
2. **OAuth scope & EndingReason** — the two unconfirmed details in
   `ebay_client.py`; need one real Sandbox call to settle.
3. **Title/description/price rules** — all match your latest instructions;
   take a look at the actual phrase pools in `title_rules.py` and
   `config.py` and adjust wording to taste.
4. **Hybrid relist-vs-new-listing strategy** — a possible refinement (use
   true `AddFixedPriceItem` for zero-watcher listings instead of relisting)
   is proposed but not built; see the project doc for details.
