# Refreshing the inventory dashboard

Runbook for the daily refresh. Two paths — use the token path if a token is
available, since it is one command and far less error-prone.

## Path A — with a Shopify Admin API token (preferred)

Needs `SHOPIFY_STORE_DOMAIN` and `SHOPIFY_ACCESS_TOKEN` (scopes:
`read_products`, `read_inventory`, `read_orders`, `read_analytics`) in the
environment.

```bash
cd dashboard
python3 fetch_inventory.py                                  # -> raw/*.json
RUN_RATE_END=<yesterday> python3 build_inventory_data.py raw # -> inventory_data.json
python3 build_inventory.py                                   # embeds into inventory.html
```

`fetch_inventory.py` defaults `RUN_RATE_END` to yesterday and cross-checks the
per-variant totals against store-level daily totals before writing. Pass the
same date to `build_inventory_data.py`.

## Path B — via the Shopify MCP connector (no token)

Same three build steps, but the raw files are gathered through MCP tools. Write
each result into `dashboard/raw/` in the shapes below, then run the two build
scripts exactly as in Path A.

**`raw/products_raw.json`** — array of Product nodes. Paginate
`mcp__Shopify__graphql_query` 100 at a time:

```graphql
query { products(first: 100, sortKey: ID, after: CURSOR) {
  pageInfo { hasNextPage endCursor }
  nodes { id title status productType totalInventory
          options { name }
          variants(first: 100) { nodes { id title inventoryQuantity price } } } } }
```

**`raw/sales_rows.json`** — array of `{day, product_variant_id, net_items_sold}`
covering the last 30 days ending yesterday, via `shopifyqlQuery`:

```graphql
query { shopifyqlQuery(query: "FROM sales SHOW net_items_sold GROUP BY product_variant_id TIMESERIES day SINCE <start> UNTIL <end>") {
  tableData { rows } parseErrors } }
```

**`raw/daily_totals.json`** — array of `{date, units, orders}` from
`FROM sales SHOW net_items_sold, orders TIMESERIES day SINCE <start> UNTIL <end>`.

### Three traps, all hit in practice

1. **ShopifyQL silently truncates at 1000 rows.** A 30-day per-variant pull
   exceeds it and returns 1000 rows with no error, dropping the most recent
   days — exactly the ones the run rates need. Pull in **4-day chunks** and
   treat any chunk returning 1000 rows as truncated.
2. **Always reconcile before building.** Sum `net_items_sold` per day across
   variants and check it equals the store-level `daily_totals` for that day. If
   a day disagrees, the pull is incomplete — do not publish it.
3. **Large MCP results are written to a file instead of being returned inline.**
   That is the good case: read them with `jq`. When a result comes back inline
   and is too big to handle comfortably, re-issue the query with duplicate
   aliases (`a:`, `b:`) to push it over the size threshold so it lands on disk.

## Verifying before commit

```bash
cd dashboard && python3 - <<'EOF'
import json; d=json.load(open("inventory_data.json")); t=d["totals"]
assert t["units_in_stock"] == sum(p["stock"] for p in d["products"])
assert len(d["daily"]) >= 7 and d["meta"]["run_rate_end"] == d["daily"][-1]["date"]
print("OK", t["units_in_stock"], "units,", t["rr7"], "units/day (7d)")
EOF
```

Then commit `inventory.html` and `inventory_data.json` to the working branch and
push. Do not open a pull request.

`raw/` and `inventory_artifact.html` are gitignored build outputs — both are
regenerated on every refresh, and committing them would add about a megabyte to
history per day. `inventory_data.json` is the committed source of truth.

## Sanity checks on the numbers

- Run-rate windows end on the **last complete day** — never include today.
- Units are **net of returns**, so a day can legitimately be zero or negative.
- If the 7-day run rate moves more than ~3x overnight, suspect the pull before
  believing the number.

## The daily schedule

A Routine, **"Elan inventory dashboard — daily refresh"**, runs this at
**01:30 UTC / 07:00 IST** every day. It is bound to a persistent Claude session
rather than firing a fresh one, because a fresh session gets no connector
(`mcp__Shopify__*`) tools and Path B would have nothing to call. The sibling
performance-dashboard routine (00:30 UTC) is set up the same way. The two are
staggered an hour apart and touch different files:

| Routine | Time | Touches |
|---|---|---|
| Refresh Elan performance dashboard | 00:30 UTC / 06:00 IST | `index.html`, `data.json` |
| Elan inventory dashboard — daily refresh | 01:30 UTC / 07:00 IST | `inventory.html`, `inventory_data.json`, `raw/` |

**Making it robust:** set `SHOPIFY_STORE_DOMAIN` and `SHOPIFY_ACCESS_TOKEN` in
the environment. Path A then needs no connector at all, so the refresh stops
depending on a session staying alive and becomes a single command.

The routine is told to stay quiet on a clean run, and to speak up only if the
pull fails, the reconciliation fails, or a product has newly dropped under 7
days of cover.

## Republishing the shared link

The dashboard is also published as an Artifact:
**https://claude.ai/code/artifact/c5dd083e-74ff-4041-8e94-86b1a2596609**

After rebuilding, regenerate the publishable variant and update the artifact
**in place**:

```bash
python3 build_artifact.py    # -> inventory_artifact.html
```

Then publish with the Artifact tool passing
`url: "https://claude.ai/code/artifact/c5dd083e-74ff-4041-8e94-86b1a2596609"`.
**Never omit `url`** — publishing without it creates a second, separate artifact
instead of updating this one, and the link you already shared goes stale.

`build_artifact.py` exists because the repo file points at `../Gilroy-*.woff`
and `../Elan - Wordmark - White.svg`. Those resolve inside the repo but 404 on
the artifact origin, where Gilroy would silently fall back to system-ui. The
script inlines them as base64 data URIs and strips the document wrapper the
Artifact tool supplies itself.
