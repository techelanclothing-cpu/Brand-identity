# Elan Clothing — Performance Dashboard

A single-file, brand-styled e-commerce dashboard for **elanclothing.in**, with
Daily / Weekly / Monthly views of:

- Gross Orders
- Gross Sale
- Gross Quantity
- Conversion %
- AOV
- Revenue (Gross Sale − Discount)
- Ad Spend
- ROAS (Revenue / Ad Spend)
- Discount %
- Total Sessions
- COD vs. Prepaid order split

plus a **Compare periods** panel (pick any two custom date ranges and see
every metric side by side with a % delta — clearer than reading it off a
trend line), a Revenue-vs-Ad-Spend trend chart, Orders and Sessions trend
charts, a COD/Prepaid breakdown, and a full sortable-by-period data table —
all in the Elan brand palette and Gilroy typeface, with light/dark mode.

## Files

| File | Purpose |
|---|---|
| `index.html` | The dashboard itself. Self-contained — the data is embedded inline, so it works by double-clicking the file, no server needed. |
| `data.json` | The metrics data, in the shape `index.html` reads. Source of truth after a refresh. |
| `fetch_metrics.py` | Pulls fresh data from Shopify (Admin GraphQL / ShopifyQL) and Meta Ads (Marketing API), and writes `data.json`. |
| `build.py` | Embeds the current `data.json` into `index.html`'s inline `<script id="dashboard-data">` block. |
| `refresh_from_mcp.py` | Alternative to `fetch_metrics.py` — builds `data.json` from raw MCP tool output (Shopify/Meta Ads connectors inside a Claude session) instead of raw HTTP API tokens. Used by the scheduled Claude Routine that keeps the dashboard current; see below. |

## Viewing it

Just open `index.html` in a browser — double-click it, or `open dashboard/index.html`.
No build step or server is required to view the current snapshot.

## It's a growing archive, not a rolling window

Every refresh re-pulls the last `DAYS_BACK` days (default 60) from Shopify/Meta
and **merges** them into `data.json`: days within that fresh window are
overwritten (so refunds, cancellations, or corrected financial status on
recent orders stay accurate), but every earlier day is left untouched. Nothing
ages out — the archive keeps growing for as long as the dashboard has been
refreshed, so the Daily/Weekly/Monthly views and the Compare panel can look
back arbitrarily far, not just the last couple of months. Both
`fetch_metrics.py` and `refresh_from_mcp.py build` share this merge logic
(`merge_daily()` / `load_existing_daily()` in `fetch_metrics.py`) — if
`data.json` doesn't exist yet, the first run just seeds the archive.

## Automatic daily refresh (GitHub Action)

`.github/workflows/refresh-dashboard.yml` runs every day at 00:30 UTC (06:00
IST), pulls fresh data, and commits `dashboard/data.json` + `dashboard/index.html`
straight to this branch — no manual step, no Claude session involved.

**One-time setup — add three repo secrets** (Settings → Secrets and variables →
Actions → New repository secret):

Shopify retired the old "Develop apps → Create an app" flow for new apps as of
January 1, 2026 (existing apps still work, but you can't create new ones that
way anymore — if you land on a "Legacy custom apps" notice in Shopify Admin,
that's why). New apps are created in the **Dev Dashboard** instead, and
authenticate with a **Client ID + Client secret** rather than a single static
token:

1. Go to **dev.shopify.com/dashboard** and log in with the account that owns
   `elan-clothing-r.myshopify.com` (or that store's organization).
2. **Apps → Create app → Start from Dev Dashboard.** Name it something like
   "Performance dashboard sync."
3. Open the app → **Versions → Release** (top right). Under **Access**, select
   scopes: add `read_orders` and `read_analytics`. Save/Release.
4. Back on the app's **Home** tab → **Install app** → choose
   `elan-clothing-r.myshopify.com` → Install.
5. Go to **Settings** on the app and copy the **Client ID** and **Client
   secret**.

| Secret | Value |
|---|---|
| `SHOPIFY_CLIENT_ID` | The Client ID from step 5. |
| `SHOPIFY_CLIENT_SECRET` | The Client secret from step 5. Treat it like a password. |
| `META_ACCESS_TOKEN` | developers.facebook.com → your app (or Meta Business Suite → Business Settings → System Users) → generate a token with `ads_read` permission for the ad account, ideally a long-lived / system-user token so it doesn't expire every 60 days. |

`fetch_metrics.py` exchanges the Shopify Client ID/secret for a fresh 24-hour
access token on every run (the "client credentials grant" — Shopify's current
way for an app to authenticate against its own store with no manual token to
rotate), so there's nothing to renew by hand later.

Once all three secrets exist, the workflow runs on its own from the next
scheduled time — no further action needed. Trigger it manually any time from
the Actions tab ("Refresh performance dashboard" → Run workflow) to test it
or force an immediate refresh.

Note: this keeps `dashboard/index.html` in the repo current, but does **not**
update a separately-published Claude Artifact link — Artifact publishing is a
Claude-only action. If you want a public URL that reflects this automatic
refresh (rather than a link someone has to manually re-publish), enable
GitHub Pages for this repo/branch, or ask Claude to re-publish the Artifact
whenever you need the latest snapshot shared that way.

## Refreshing the data manually

The dashboard ships with a real snapshot (61 days, 2026-06-25 to 2026-08-24) pulled
live from the connected Shopify store and Meta Ads account. To pull a fresh snapshot:

```bash
export SHOPIFY_STORE_DOMAIN=elan-clothing-r.myshopify.com
export SHOPIFY_CLIENT_ID=...           # from the Dev Dashboard app's Settings page
export SHOPIFY_CLIENT_SECRET=...       # same app, keep this one secret
export META_AD_ACCOUNT_ID=1778418969517591    # numeric ad account id, no "act_" prefix
export META_ACCESS_TOKEN=EAA...                # Marketing API token: ads_read

python3 fetch_metrics.py   # writes data.json
python3 build.py           # embeds data.json into index.html
```

See the Dev Dashboard walkthrough above for how to get `SHOPIFY_CLIENT_ID` /
`SHOPIFY_CLIENT_SECRET` — the old single-token "Develop apps" flow was retired
January 1, 2026.

`DAYS_BACK` (default `60`) controls how much history `fetch_metrics.py` pulls.

Re-run this pair on a schedule (cron, GitHub Action, etc.) to keep the dashboard
current — commit the refreshed `index.html` and `data.json` afterwards.

### Refreshing without API tokens (Claude session / Routine)

If you don't want to manage raw Shopify/Meta API tokens, `refresh_from_mcp.py`
builds the same `data.json` from the Shopify and Meta Ads **MCP connector**
tools available inside a Claude session instead:

1. Call `mcp__Shopify__run-analytics-query` for the sales and sessions
   ShopifyQL queries, and `mcp__Meta_ads__ads_get_ad_entities` for ad spend —
   exact queries are documented in the docstring at the top of the script.
2. For the COD/Prepaid split, run `python3 refresh_from_mcp.py gen-cod-query
   --days-back 60` to print the GraphQL query text (chunked into ~15-day
   batches), call `mcp__Shopify__graphql_query` with each chunk, and save
   each raw result to its own file.
3. Dump every raw tool result to a JSON file, then run:
   ```bash
   python3 refresh_from_mcp.py build --sales sales.json --sessions sessions.json \
     --adspend adspend.json --cod-counts cod_chunk1.json cod_chunk2.json ...
   python3 build.py
   ```

This is what the scheduled "refresh the Elan dashboard" Claude Routine does
daily — it re-runs those MCP calls, rebuilds `data.json`, commits and pushes
`dashboard/index.html` + `dashboard/data.json`, and republishes the dashboard
Artifact. It's bound to a specific Claude session that already has the
Shopify/Meta Ads connectors authorized (not a fresh session per run) — this
org has connector-passing to fresh scheduled sessions disabled, so binding to
an existing authorized session is what makes this work without a Shopify app.

## How each metric is computed

- **Gross Orders / Gross Sale / Discounts / AOV** — Shopify's ShopifyQL `sales`
  dataset (`orders`, `gross_sales`, `discounts`, `average_order_value`).
- **Gross Quantity** — Shopify's `net_items_sold` metric (units sold, net of
  returns) for the period.
- **Total Sessions** — Shopify's ShopifyQL `sessions` dataset.
- **Conversion %** — `Orders / Sessions × 100` for the period.
- **Revenue** — `Gross Sale − Discounts`.
- **Discount %** — `Discounts / Gross Sale × 100`.
- **Ad Spend / ROAS** — Meta Marketing API `spend` for the connected ad account;
  `ROAS = Revenue / Ad Spend`. Shown as "—" for periods with no ad spend.
- **COD vs. Prepaid split** — classified per order from Shopify's own
  financial status: **Paid → Prepaid** (paid in full online at checkout),
  **Partially Paid → COD** (a token amount collected online, balance due on
  delivery — how this store's COD flow works). Orders in other states
  (pending, refunded, voided, ...) aren't counted in the split. Computed
  exactly per day (not sampled) via one aliased `ordersCount` GraphQL query
  per ~15-day batch — see `fetch_cod_split()` in `fetch_metrics.py` and
  `cod_query_chunks()` / `parse_cod_counts()` in `refresh_from_mcp.py` —
  then rolled up into weekly/monthly totals.
- **Weekly** buckets are ISO weeks (Monday–Sunday). **Monthly** buckets are
  calendar months. A period at the edge of the pulled date range that doesn't
  have a full week/month of data is flagged "(partial)" in the dashboard and
  marked with `*` in the data table.

## Design notes

- Colors are derived from the brand palette (`Color Palette.png`): Dark Maroon
  `#5B0017`, Bronze `#A67A4F`, Slate Gray `#36454F`, Charcoal `#212121`, Soft
  Grey `#F0F0F0`. Chart line/bar colors are brand-derived but re-stepped in
  lightness/chroma to pass WCAG contrast and colorblind-safe (CVD) separation
  checks for data marks — the raw brand swatches are too dark/desaturated to
  use directly as chart marks. Light and dark variants are both validated.
- Typeface is Gilroy (the brand font, `Gilroy-*.woff` in the repo root),
  loaded via `@font-face` with `system-ui` fallback.
- No external libraries or CDN dependencies — charts are hand-drawn inline
  SVG. Works fully offline.
