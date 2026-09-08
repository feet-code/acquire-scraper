# Acquire.com → Magic Catalog

Slow direct browser scraping of buyer-visible SaaS descriptions, fixed-batch Gemini generation, and scalable catalog publishing. Anonymous listings work without unlocking a website or company name. No Acquire API is used. The scraper reads the product-description block, not data rooms, seller contacts, or private financial documents. It never contacts sellers or buys upgrades.

## First-time setup (Windows PowerShell)

```powershell
git clone https://github.com/feet-code/acquire-scraper.git
cd acquire-scraper
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m playwright install chromium
Copy-Item .env.example .env
acquire-magic-import login
```

Sign into Acquire in the opened browser, then press Enter in the terminal. The profile is saved under `.state/browser-profile`; no password is embedded in source or `.env`. Repeat login only when the buyer session expires. A ChatGPT cloud-browser login does not transfer to your PC.

Python 3.11+ is required. On macOS/Linux use `python3 -m venv .venv`, `source .venv/bin/activate`, and `cp .env.example .env`.

Discovery follows Acquire's **See more** pagination control or its desktop `.load-more-block` scroll trigger, waiting for the rendered cards to change before advancing. It saves each card and deduplicates by both components of the listing URL. Defaults include SaaS, AI, and Shopify app cards. Use `--types saas` to narrow or `--types "saas,ai,shopify app,mobile,crypto"` to expand. Clear restrictive account filters in All listings for broader coverage.

### If an older version stopped at 13

The previous loader did not recognize **See more** and could scroll past the desktop loading trigger. Update and resume; keep `.state` and your saved login:

```powershell
git pull
python -m pip install -e .
acquire-magic-import run --stage scrape --limit 10000
```

`--limit 10000` is the total eligible-listing target, including saved listings. It does not cap discovery at the first results screen. Logs report rendered cards, eligible cards, unique URLs seen, saved count, and the pagination action. `.state/discovery.json` stores the latest coverage and stop reason without page content or credentials.

Five unsuccessful page advances (each allowing up to 20 seconds for loading), or `--max-rounds`, stop discovery with an explicit incomplete-coverage warning and a nonzero exit code. Already discovered listings are still scraped and checkpointed. This avoids reporting a stalled page as a successful full crawl. Rerunning traverses the results again and deduplicates; it does not regenerate or republish completed products. A limit below your saved count needs no new discovery. Account filters, accessible inventory, and the selected card types determine coverage; the scraper cannot promise a particular inventory count or confirm exhaustion from an unchanged screen alone.

Source actions normally wait 8–12 seconds; `--delay` has a minimum of five seconds. `--jitter` increases spacing and `--headless` reuses the saved local session without a visible window. Session expiry, challenges and rate limits stop safely; source Retry-After cooldowns are persisted. A missing product-description selector fails instead of generating from an upgrade prompt. No stealth or access-control bypasses are used.

## Upgrade to batching

```powershell
git pull
python -m pip install -e .
```

Keep your existing `.state` directory. Completed scrapes, generated records, and published products are retained. Existing Product Hunt drafts get a shared general-software intent when none was previously stored; they do not consume a Gemini call just to migrate. Already published legacy products remain published and are not copied into the new storage path.

Before publishing, update **magic-catalog** too:

```powershell
git pull
npm ci
npm run deploy
```

If scalable resources have never been created, first run `npm run scale:setup` in magic-catalog. Its updated ingest endpoint reports measured D1 writes. The importers check that capability before sending products and stop if the old version is deployed. They use **ADMIN_REINDEX_TOKEN**, not the legacy ADMIN_IMPORT_TOKEN.

## Three independent queues

```powershell
# Scrape only: no Gemini key or catalog token needed.
acquire-magic-import run --stage scrape --limit 3

# Generate from saved sources: no source-site requests.
acquire-magic-import run --stage generate --limit 3

# Publish saved products: no Gemini key or source-site requests.
acquire-magic-import run --stage publish --limit 3
```

Review `.state/products.jsonl` after generation. These are compact public product records that Magic Catalog renders into pages, not HTML or copies of research pages.

Scale up by raising the total selected limit:

```powershell
acquire-magic-import run --stage scrape --limit 100000
acquire-magic-import run --stage generate --limit 100000
acquire-magic-import run --stage publish --limit 100000
```

Or keep the one-command workflow:

```powershell
acquire-magic-import run --limit 100000 --publish
```

`run` first scrapes the selected inventory, then generates from the checkpoint, then optionally publishes. Gemini quota exhaustion pauses generation, but any valid generated products are still eligible for publishing. Use `--stage scrape` whenever you want to continue collecting source data independently. The default limit is still 3. A larger limit is a ceiling, not a guarantee the source contains that many distinct accessible products.

`--scrape-only` is an alias for `--stage scrape`. `--offline` prevents source-site requests. Ctrl+C preserves completed stages; rerun the same command to resume. These are local CLI commands, not unattended scheduled jobs; daily quota resets do not restart a stopped process automatically.

## Fixed Gemini batches

Generation uses **50 products per request**, every full batch. It ignores the old saved adaptive size, and neither successful responses, partial validation failures, nor HTTP errors change the configured batch size. Only the final remainder (including unfinished retries) can contain fewer products. Set a different fixed size explicitly with `--batch-size` if needed. The old `--max-batch-size` option is accepted for compatibility but has no effect.

- HTTP 503 triggers fallback with the **same products and payload**, plus a persisted cooldown of at least five minutes (or a longer Retry-After). It indicates temporary service unavailability; it is not treated as daily quota exhaustion.
- Valid products are saved immediately. Failed validation items return to the end of the pending queue, joining other pending products in full batches where possible. Only unfinished items retry, up to three validation attempts per run.
- RPM, TPM, daily request limits, and persisted cooldowns still apply. If a full batch exceeds configured `GEMINI_TPM`, generation pauses before sending it; it does **not** silently split into smaller calls. Choose a smaller explicit batch size, or correct the TPM setting only if your AI Studio project actually permits more.
- Source-brand, copied-phrase, ID, and field validation remain enabled. No paid Batch API is used.

```powershell
acquire-magic-import run --stage generate --limit 100000
```

### Generate and publish saved scrapes together

```powershell
acquire-magic-import run --stage generate-publish --limit 100000
```

Equivalent: `run --stage generate --limit 100000 --publish`. Both use saved scrapes without opening the source site or rediscovering listings. They generate first, then publish available products; if generation pauses for quota or service availability, already generated products are still published. This is sequential, not concurrent. Ctrl+C preserves generated products; rerun to continue or use `--stage publish` to publish them directly.

`run --limit 100000 --publish` still uses the full scrape → generate → publish flow and can perform more discovery. Add `--offline` to that command to use saved sources only, or use the explicit `generate-publish` stage above.

## Shared, restart-safe quotas

Both repositories default to **the same SQLite ledger** at `~/.magic-catalog/quotas.sqlite3` (your user home directory). Running them on the same computer and OS user shares Gemini request accounting, cooldowns, intent registration, and publishing budgets. Each scraper still has its own `.state` research checkpoint.

Set these `.env` values to your actual AI Studio limits; defaults are conservative assumptions, not guaranteed Google quotas:

```text
GEMINI_RPD=20
GEMINI_RPM=5
GEMINI_TPM=25000
GEMINI_QUOTA_SCOPE=default-project
MAGIC_WRITE_SCOPE=cloudflare-account
```

The limits above apply per configured model. The input-token estimate uses serialized request size; server 429s remain authoritative. The ledger reserves attempts **before** sending them, including failed requests. It saves model cooldowns, honors Retry-After, treats recognized daily-quota errors as unavailable until midnight Pacific, and skips missing models for 24 hours. Quota changes and cooldowns survive Ctrl+C and restarts.

Temporary availability waits are bounded to two minutes by default, with short interruptible waits. Use `--wait-minutes 0` to stop immediately when no model is ready. Daily exhaustion prints the next eligible time and leaves unfinished items queued. Model/key errors do not poison every remaining source row.

```powershell
acquire-magic-import quota-status
acquire-magic-import status
acquire-magic-import retry-failed
```

If you customize `MAGIC_QUOTA_DB`, give both repositories the **same absolute file path**. `GEMINI_QUOTA_SCOPE` identifies the actual Google project; do not change it merely to reset quotas. Two different API keys for the same project still need the same scope. Other programs and separate computers do not automatically share this ledger; leave headroom for their usage. Run at most one process per scraper checkpoint.

## Measured scalable publishing

Set the following in each scraper's `.env`:

```text
GEMINI_API_KEY=your-key
MAGIC_CATALOG_URL=https://magic-catalog.cloudwebsites.workers.dev
MAGIC_CATALOG_IMPORT_TOKEN=the-same-value-as-the-Worker-ADMIN_REINDEX_TOKEN
```

Publishing uses `/api/admin/catalog/ingest`: R2 product bodies, sharded D1 metadata and FTS, and **59 shared intent vectors**. It never creates a vector per imported product. Source research and credentials are not sent in public product records.

Generation batch size and import batch size are independent. The CLI permits an import ceiling of 25, but automatically honors the server's lower advertised limit, currently **7 products/request**. The current database statements plus intent and object writes need this smaller batch to leave room under the Worker Free request limits. Intent definitions are sent once per destination in the shared ledger.

The default publishing allowance is **80,000 D1 rows/day shared between both importers**, leaving nominal headroom below D1 Free's 100,000. This counts measured metadata, index, FTS, and intent writes reported by D1; it does not assume one product equals one row.

Before the first measured batch, reserve 100 rows per product. Afterwards estimate using the highest observed rows per product plus 50% and four extra rows. Successful batches reconcile reservations to actual reported usage. Failed/uncertain batches retain their conservative reservations; their exact saved products remain queued for idempotent retry. A missing acknowledgement or missing usage report never marks products as published. This is a local planning budget, not a guarantee against unobserved account traffic or an unexpectedly expensive query.

```powershell
acquire-magic-import run --stage publish --limit 100000 --daily-row-budget 60000
```

The budget resets at midnight UTC. Other applications on the account consume the same Cloudflare free allowance, so lower this budget if necessary. Renamed products use stable source-derived slugs; replaying a partially successful batch upserts the same products. Preserve both the research checkpoint and shared ledger for reliable progress/quota accounting.

If vector indexing was unavailable, `npm run vector:reindex` in Magic Catalog repairs persisted intents. Neither scraper changes Cloudflare billing or provisions paid resources.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Tests cover partial/truncated responses, wrong and duplicate IDs, fixed batch sizes and 503 fallback, model fallback, persisted daily/minute quotas, Pacific daylight-saving resets, intent reuse, server-advertised import caps, measured write accounting, partial failures, stable identities, and preservation of existing checkpoints.
