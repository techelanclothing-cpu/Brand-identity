# Elan Clothing — Performance Dashboard

A single-file, brand-styled e-commerce dashboard for **elanclothing.in**, with
Daily / Weekly / Monthly views of:

- Gross Orders
- Gross Sale
- Gross Quantity
- Conversion %
- AOV
- Revenue (Gross Sale − Discount)
- ROAS (Revenue / Ad Spend)
- Discount %
- Total Sessions
- COD vs. Prepaid order split (by order count and by value)

plus a Revenue-vs-Ad-Spend trend chart, Orders and Sessions trend charts, a
COD/Prepaid breakdown, and a full sortable-by-period data table — all in the
Elan brand palette and Gilroy typeface, with light/dark mode.

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

## Automatic daily refresh (GitHub Action)

`.github/workflows/refresh-dashboard.yml` runs every day at 00:30 UTC (06:00
IST), pulls fresh data, and commits `dashboard/data.json` + `dashboard/index.html`
straight to this branch — no manual step, no Claude session involved.

**One-time setup — add two repo secrets** (Settings → Secrets and variables →
Actions → New repository secret):

| Secret | How to get it |
|---|---|
| `SHOPIFY_ACCESS_TOKEN` | Shopify Admin → Settings → Apps and sales channels → Develop apps → Create an app → Configuration → Admin API scopes: enable `read_orders` and `read_analytics` → Install app → API credentials → reveal the Admin API access token (`shpat_...`). |
| `META_ACCESS_TOKEN` | developers.facebook.com → your app (or Meta Business Suite → Business Settings → System Users) → generate a token with `ads_read` permission for the ad account, ideally a long-lived / system-user token so it doesn't expire every 60 days. |

Once both secrets exist, the workflow runs on its own from the next scheduled
time — no further action needed. Trigger it manually any time from the
Actions tab ("Refresh performance dashboard" → Run workflow) to test it or
force an immediate refresh.

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
export SHOPIFY_ACCESS_TOKEN=shpat_...        # Admin API token: read_orders, read_analytics
export META_AD_ACCOUNT_ID=1778418969517591    # numeric ad account id, no "act_" prefix
export META_ACCESS_TOKEN=EAA...                # Marketing API token: ads_read

python3 fetch_metrics.py   # writes data.json
python3 build.py           # embeds data.json into index.html
```

`DAYS_BACK` (default `60`) controls how much history `fetch_metrics.py` pulls.

Re-run this pair on a schedule (cron, GitHub Action, etc.) to keep the dashboard
current — commit the refreshed `index.html` and `data.json` afterwards.

### Refreshing without API tokens (Claude session / Routine)

If you don't want to manage raw Shopify/Meta API tokens, `refresh_from_mcp.py`
builds the same `data.json` from the Shopify and Meta Ads **MCP connector**
tools available inside a Claude session instead:

1. Call `mcp__Shopify__run-analytics-query` for the sales and sessions
   ShopifyQL queries, `mcp__Meta_ads__ads_get_ad_entities` for ad spend, and
   `mcp__Shopify__graphql_query` (paginated) for order payment gateways —
   exact queries are documented in the docstring at the top of the script.
2. Dump each raw tool result to a JSON file.
3. Run:
   ```bash
   python3 refresh_from_mcp.py --sales sales.json --sessions sessions.json \
     --adspend adspend.json --orders orders_p1.json orders_p2.json ...
   python3 build.py
   ```

This is what the scheduled "refresh the Elan dashboard" Claude Routine does
daily — it re-runs those MCP calls, rebuilds `data.json`, commits and pushes
`dashboard/index.html` + `dashboard/data.json`, and republishes the dashboard
Artifact.

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
- **COD vs. Prepaid split** — each order's `paymentGatewayNames` from the
  Shopify Admin API: an order is COD if it includes `"Cash on Delivery (COD)"`,
  otherwise it's counted as Prepaid (Razorpay, Shopflo, cards, UPI, wallets,
  etc). Computed both by order count and by order value, then rolled up into
  weekly/monthly averages. `fetch_metrics.py` computes this per day by
  paginating recent orders — see `fetch_cod_split()`.
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
