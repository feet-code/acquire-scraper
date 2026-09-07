# Acquire.com → Magic Catalog

Direct, slow browser scraping of Acquire buyer-visible SaaS listings, Gemini-generated product pages, and restart-safe publishing to Magic Catalog's **new scalable architecture**:

- `/api/admin/catalog/ingest` authenticated with **ADMIN_REINDEX_TOKEN**
- full product JSON in R2, compact metadata and FTS in the D1 search shards
- **59 shared intent definitions**, not a new vector for each product

No Acquire API credentials are needed. Company website/name unlocks are not needed: the visible product description is enough. The scraper reads the product-description block only; it does not open data rooms, contact sellers, buy an upgrade, or scrape private financial documents. It does not fetch external product websites.

## Setup (Windows PowerShell)

```powershell
git clone https://github.com/feet-code/acquire-scraper.git
cd acquire-scraper
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m playwright install chromium
Copy-Item .env.example .env
```

On macOS/Linux use `python3 -m venv .venv`, `source .venv/bin/activate`, and `cp .env.example .env`; the remaining commands are the same. Python 3.11+ is required.

### Sign in once

```powershell
acquire-magic-import login
```

A normal Chromium window opens. Enter your buyer email and password **in that window**, then press Enter in the terminal. The command verifies access to the listings and saves the browser profile locally under `.state/browser-profile`. The profile includes session storage such as cookies and IndexedDB. No password is embedded in code or `.env`; do not commit or share the profile.

Login from a ChatGPT cloud browser does not transfer to the browser on your PC. Run this command once locally, and again only when your session expires.

## Test three scrapes first (no Gemini key needed)

```powershell
acquire-magic-import run --limit 3 --scrape-only
acquire-magic-import status
```

This saves three product descriptions in `.state/scraper.sqlite3`. Then put your Gemini key into `.env`:

```text
GEMINI_API_KEY=...
```

Generate three product pages without publishing:

```powershell
acquire-magic-import run --limit 3
```

Review `.state/products.jsonl`. These records contain the full product-page fields Magic Catalog renders: name, audience, problem, promise, differentiator, workflow, keywords, and metrics. Original source text stays in the local checkpoint, not the public record.

The Gemini model order matches Product Hunt:

```text
gemini-3.8-flash
gemini-3.7-flash
gemini-3.6-flash
gemini-3.5-flash
gemini-3-flash
gemini-2.5-flash
```

These are attempted IDs, not a guarantee Google enables all of them for your key. Unavailable models, quota errors, malformed output, and validation failures fall through. If the entire chain fails, the run stops with the scraped input saved. Rerun after resolving the key/quota issue. There is no Cloudflare AI generation fallback.

Generation writes fresh copy based on the described user problem and workflow, chooses a new name, and rejects copied seven-word phrases and duplicate generated names. The prompt excludes seller identities, URLs, source sale figures, and unsupported business claims. Names hidden by Acquire remain unknown; the scraper does not attempt to uncover them.

## Publish the three test products

Magic Catalog must have its scalable resources deployed. In **magic-catalog**, if you have not already completed scale setup:

```powershell
npm run scale:setup
npx wrangler secret put ADMIN_REINDEX_TOKEN
npm run deploy
```

Use the existing ADMIN_REINDEX_TOKEN if already configured; you do not need to replace it. In **acquire-scraper/.env**:

```text
MAGIC_CATALOG_URL=https://magic-catalog.cloudwebsites.workers.dev
MAGIC_CATALOG_IMPORT_TOKEN=the-same-value-as-ADMIN_REINDEX_TOKEN
```

This is **not** the old Product Hunt `ADMIN_IMPORT_TOKEN` endpoint/token.

```powershell
acquire-magic-import run --limit 3 --publish
```

Product pages appear at `/product/<slug>` on your catalog. Slugs are stable `acq-<listing-hash>` identifiers. A retry—even after regeneration—upserts the same product instead of duplicating it or overwriting a different scraper's product.

## Expand to everything accessible

```powershell
acquire-magic-import run --limit 10000 --publish
```

`--limit` is the **total first N eligible listings in this checkpoint**, including completed ones. Increasing from 3 to 10,000 processes the remaining items. Rerunning the same command skips completed stages. Raise the limit if more listings are available; 10,000 is a requested ceiling, not a claim about how many Acquire exposes.

Discovery scans the all-listings page and scrolls incrementally as more cards load. It checkpoints every matching card, then processes the selected records. It includes `SaaS`, `AI`, and `Shopify app` cards by default; this avoids missing SaaS listed under AI. You can restrict to `--types saas`, or include `mobile,crypto` explicitly:

```powershell
acquire-magic-import run --limit 10000 --types "saas,ai,shopify app,mobile,crypto" --publish
```

Discovery ends when no new cards appear across five paced checks or `--max-rounds` is reached. It reports actual coverage, not a claim that all historical/sold/locked listings were found. If it stops short because of slow loading, rerun; it rescans from the top and deduplicates. Existing account filters can limit coverage; clear restrictive filters on the All listings page during login.

## Pause, resume, and troubleshoot

Ctrl+C is safe. Run the same command to resume; keep `.state` to preserve generated names, intent registration, and budget counters.

```powershell
# Counts plus the first 20 per-item errors
acquire-magic-import status

# Retry item-specific extraction failures, retaining successful work
acquire-magic-import retry-failed
acquire-magic-import run --limit 10000 --publish

# Regenerate/export from saved sources without opening Acquire
acquire-magic-import run --limit 10000 --offline
acquire-magic-import export --output .state/review.jsonl

# Slow the crawler further or run the saved browser session headlessly
acquire-magic-import run --limit 10000 --delay 12 --jitter 5 --headless --publish
```

- Source actions are sequential, normally spaced **8–12 seconds** apart; `--delay` cannot go below 5 seconds. The browser also loads the site's ordinary assets and background requests.
- Source 429 responses stop the run; retry cooldowns are persisted. Sign-in expiry, challenges, and access denials stop without bypass attempts.
- Gemini HTTP 429/temporary server responses honor Retry-After and bounded backoff.
- Publishing uses one product per request, at least 8 seconds between requests, and a persistent default limit of **5,000 ingest attempts per UTC day**. Failed/partial writes consume the budget too. This is conservative headroom, not a global Cloudflare quota meter: other importers and site traffic share your quotas.
- Each intent is supplied once per checkpoint. The catalog persists intents even if its vector write fails; repair vectors with `npm run vector:reindex` in Magic Catalog.
- A publish error stops immediately and preserves the exact pending payload. Rerun to retry; no `retry-failed` command is needed for publish failures or a Gemini-wide outage.
- HTTP 404 from ingest means wrong URL/token or undeployed endpoint. HTTP 503 usually means scale resources are not deployed. Runtime errors name the failed stage without dumping credentials or page HTML.
- A changed Acquire layout fails visibly rather than generating pages from navigation or upgrade text. Selectors live in `src/acquire_scraper/extract.py`.

## Validation

```powershell
python -m unittest discover -s tests -v
```

Tests cover URL identity, anonymous listings, category filtering, copied-brand rejection, model fallback, intent validation, partial publish recovery, acknowledgement checking, daily budgets, checkpoint resumption, and credential-bearing redirect refusal.

The implementation was checked against the live authenticated All listings page and anonymous SaaS description layout on September 7, 2026. Live Gemini generation and production ingestion additionally require your API key and catalog admin token.

Gemini validation and the bounded HTTP client are adapted from your `feet-code/product-hunt-scraper` implementation; discovery, persistence, and scalable publishing are specific to this repository.
