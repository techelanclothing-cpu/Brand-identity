#!/usr/bin/env python3
"""
Pulls a fresh inventory + run-rate snapshot from Shopify and writes the three
raw files that build_inventory_data.py consumes.

Pulls:
  - Admin GraphQL `products`: every product with its size variants and
    inventoryQuantity (stock on hand, summed across locations) and price
  - ShopifyQL `sales`: net_items_sold per product_variant_id per day, for the
    run-rate window
  - ShopifyQL `sales`: store-level net_items_sold and orders per day, for the trend

Environment variables required:
  SHOPIFY_STORE_DOMAIN     e.g. "elan-clothing-r.myshopify.com"
  SHOPIFY_ACCESS_TOKEN     Admin API token with read_products, read_inventory,
                           read_orders, read_analytics

Optional:
  TREND_DAYS               days of store-level history for the trend (default 30)
  RUN_RATE_END             last complete day, YYYY-MM-DD (default: yesterday)

Usage:
  python3 fetch_inventory.py            # writes raw files into ./raw
  python3 build_inventory_data.py raw   # -> inventory_data.json
  python3 build_inventory.py            # embeds it into inventory.html
"""
import os
import sys
import json
import datetime
import urllib.request

SHOPIFY_API_VERSION = "2025-01"
# ShopifyQL truncates a result set at 1000 rows, so per-variant daily pulls are
# chunked into short date ranges and stitched back together.
CHUNK_DAYS = 4


def env(name, required=True, default=None):
    v = os.environ.get(name, default)
    if required and not v:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return v


def shopify_graphql(domain, token, query, variables=None):
    url = f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Shopify-Access-Token", token)
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if "errors" in payload:
        raise RuntimeError(f"Shopify GraphQL error: {payload['errors']}")
    return payload["data"]


def shopifyql(domain, token, query):
    gql = """
    query ShopifyQL($query: String!) {
      shopifyqlQuery(query: $query) {
        tableData { columns { name dataType } rows }
        parseErrors
      }
    }
    """
    data = shopify_graphql(domain, token, gql, {"query": query})
    result = data["shopifyqlQuery"]
    if result.get("parseErrors"):
        raise RuntimeError(f"ShopifyQL error for {query!r}: {result['parseErrors']}")
    table = result.get("tableData")
    return table["rows"] if table else []


PRODUCTS_QUERY = """
query Products($cursor: String) {
  products(first: 100, sortKey: ID, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title status productType totalInventory
      options { name }
      variants(first: 100) { nodes { id title inventoryQuantity price } }
    }
  }
}
"""


def fetch_products(domain, token):
    out, cursor = [], None
    while True:
        data = shopify_graphql(domain, token, PRODUCTS_QUERY, {"cursor": cursor})
        conn = data["products"]
        out.extend(conn["nodes"])
        print(f"  ...{len(out)} products")
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return out


def fetch_variant_daily(domain, token, start, end):
    """Per-variant daily units, pulled in short chunks to stay under the
    1000-row ShopifyQL cap."""
    rows = []
    cur = start
    while cur <= end:
        stop = min(cur + datetime.timedelta(days=CHUNK_DAYS - 1), end)
        chunk = shopifyql(
            domain, token,
            f"FROM sales SHOW net_items_sold GROUP BY product_variant_id "
            f"TIMESERIES day SINCE {cur.isoformat()} UNTIL {stop.isoformat()}",
        )
        if len(chunk) >= 1000:
            raise RuntimeError(
                f"ShopifyQL returned {len(chunk)} rows for {cur}..{stop} — at the "
                "1000-row cap, so the window is truncated. Lower CHUNK_DAYS."
            )
        rows.extend(chunk)
        print(f"  ...{cur} to {stop}: {len(chunk)} rows")
        cur = stop + datetime.timedelta(days=1)
    return rows


def fetch_daily_totals(domain, token, start, end):
    rows = shopifyql(
        domain, token,
        f"FROM sales SHOW net_items_sold, orders TIMESERIES day "
        f"SINCE {start.isoformat()} UNTIL {end.isoformat()}",
    )
    return [
        {"date": r["day"][:10], "units": int(r["net_items_sold"]), "orders": int(r["orders"])}
        for r in rows
    ]


def main():
    domain = env("SHOPIFY_STORE_DOMAIN")
    token = env("SHOPIFY_ACCESS_TOKEN")
    trend_days = int(os.environ.get("TREND_DAYS", "30"))

    end = os.environ.get("RUN_RATE_END")
    end = (datetime.date.fromisoformat(end) if end
           else datetime.date.today() - datetime.timedelta(days=1))
    trend_start = end - datetime.timedelta(days=trend_days - 1)
    # The run rates need the last 7 complete days; pull the whole trend window
    # so the same file also backs the daily chart.
    rr_start = min(trend_start, end - datetime.timedelta(days=6))

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw")
    os.makedirs(out_dir, exist_ok=True)

    print("Fetching products and inventory...")
    products = fetch_products(domain, token)

    print(f"Fetching per-variant daily sales {rr_start} to {end}...")
    sales_rows = fetch_variant_daily(domain, token, rr_start, end)

    print(f"Fetching store-level daily totals {trend_start} to {end}...")
    daily = fetch_daily_totals(domain, token, trend_start, end)

    for name, payload in (("products_raw", products), ("sales_rows", sales_rows), ("daily_totals", daily)):
        path = os.path.join(out_dir, name + ".json")
        with open(path, "w") as f:
            json.dump(payload, f)
        print(f"Wrote {path}")

    # Cross-check: per-variant units must reconcile with the store-level totals.
    by_day = {}
    for r in sales_rows:
        if r.get("product_variant_id"):
            by_day[r["day"][:10]] = by_day.get(r["day"][:10], 0) + int(r["net_items_sold"])
    mismatch = [d for d in by_day if any(x["date"] == d and x["units"] != by_day[d] for x in daily)]
    if mismatch:
        print(f"WARNING: per-variant totals differ from store totals on {sorted(mismatch)} "
              "— usually unattributed adjustments; check before trusting the run rates.")
    else:
        print("Per-variant sales reconcile with store-level daily totals.")

    print(f"\nNext: RUN_RATE_END={end.isoformat()} python3 build_inventory_data.py {out_dir}")
    print("Then: python3 build_inventory.py")


if __name__ == "__main__":
    main()
