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

## Viewing it

Just open `index.html` in a browser — double-click it, or `open dashboard/index.html`.
No build step or server is required to view the current snapshot.

## Refreshing the data

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

---

# Elan Clothing — Inventory Dashboard

A second single-file, brand-styled dashboard (`inventory.html`) covering **stock
on hand** and **how fast it is selling**:

- Units in stock, sizes in stock vs. out of stock, stock value at retail
- **Daily run rate over 1-day, 3-day and 7-day windows** — units sold per day
- Days of cover (stock ÷ run rate) at catalogue, size and product level
- Units-sold-per-day trend over 30 days, against the 7-day average
- Stock mix vs. demand mix by size — which sizes are over- or under-weighted
- A reorder watchlist of selling products with under 14 days of cover
- Every product with its **per-size stock**, searchable, filterable and sortable

## Files

| File | Purpose |
|---|---|
| `inventory.html` | The dashboard. Self-contained — data is embedded inline, so it works by double-clicking. |
| `inventory_data.json` | The inventory + run-rate data, in the shape `inventory.html` reads. |
| `fetch_inventory.py` | Pulls products, per-variant daily sales and store daily totals from Shopify into `raw/`. |
| `build_inventory_data.py` | Turns `raw/` into `inventory_data.json` (run rates, cover, size rollups). |
| `build_inventory.py` | Embeds `inventory_data.json` into `inventory.html`. |

## Refreshing the data

```bash
export SHOPIFY_STORE_DOMAIN=elan-clothing-r.myshopify.com
export SHOPIFY_ACCESS_TOKEN=shpat_...   # read_products, read_inventory, read_orders, read_analytics

python3 fetch_inventory.py                    # -> raw/*.json
RUN_RATE_END=2026-09-09 python3 build_inventory_data.py raw   # -> inventory_data.json
python3 build_inventory.py                    # embeds into inventory.html
```

`TREND_DAYS` (default `30`) controls the trend window. `RUN_RATE_END` defaults to
yesterday; `build_inventory_data.py` reads the same variable to set the windows.

## How each metric is computed

- **Stock** — Shopify's `inventoryQuantity` per size variant, summed across all
  locations (Bangalore, Calicut, Shop location). Every product in this catalogue
  uses a single `Size` option, so one variant is exactly one size.
- **Units sold** — ShopifyQL's `net_items_sold` (units **net of returns**),
  grouped by `product_variant_id` per day. A heavy-return day can therefore
  show a negative figure for a product, and did on 1 Sept 2026, when returns
  cancelled the day's units out to zero.
- **Daily run rate** — units sold in the window ÷ days in the window, for 1-, 3-
  and 7-day windows. Windows **end on the last complete day**; today is excluded,
  because a part-day drags every rate down. The 7-day rate is the steadiest and
  is what days-of-cover uses.
- **Days of cover** — units in stock ÷ the 7-day daily run rate. Items with no
  sales in the last 7 days have no meaningful rate and show "—" rather than
  infinity. Bands: under 7 days *reorder now*, 7–14 *low*, 14–30 *watch*,
  over 30 *healthy*. Each band ships with a written label, so colour never
  carries the meaning alone.
- **Stock value** — units in stock × current retail price. It is sell-through
  value, not cost.
- **Stock mix vs. demand mix** — each size's share of total units in stock
  against its share of units sold in the last 7 days. A size whose stock share
  runs well ahead of its demand share is overweight.

### A note on the 1000-row cap

ShopifyQL truncates a result set at 1000 rows with no error. A per-variant daily
pull over 30 days exceeds that, silently losing the most recent days. So
`fetch_inventory.py` chunks the pull into 4-day ranges, raises if any chunk comes
back at the cap, and cross-checks the stitched per-variant totals against the
store-level daily totals before writing.

## Design notes

- Same brand system as `index.html`: Elan palette, Gilroy, light/dark, inline SVG
  charts, no libraries or CDN dependencies.
- The two chart series (`#B3264F` / `#B07A22` light, `#E0567F` / `#B57C2C` dark)
  were validated for lightness band, chroma, colourblind separation, normal-vision
  separation and contrast against both surfaces — all-pairs pass in both modes.
