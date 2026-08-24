#!/usr/bin/env python3
"""
Regenerates dashboard/data.json from the *raw JSON responses* of the Shopify
and Meta Ads MCP tools available inside a Claude session (Shopify connector,
Meta Ads connector) — no API tokens required, unlike fetch_metrics.py which
talks to the raw HTTP APIs directly.

This is the script the scheduled "refresh the dashboard" Routine uses: a
fresh Claude session calls the MCP tools listed below, dumps each raw result
to a JSON file, then runs this script to turn those dumps into data.json.

Required input files (paths passed as CLI args, see --help):

1. --sales <path>
   Raw output of mcp__Shopify__run-analytics-query for:
     FROM sales SHOW orders, gross_sales, discounts, total_sales,
       average_order_value, net_items_sold TIMESERIES day SINCE -60d UNTIL today
   i.e. the tool's JSON result verbatim: {"columns": [...], "rows": [...], ...}

2. --sessions <path>
   Raw output of mcp__Shopify__run-analytics-query for:
     FROM sessions SHOW sessions TIMESERIES day SINCE -60d UNTIL today
   Same verbatim shape.

3. --adspend <path>
   Raw output of mcp__Meta_ads__ads_get_ad_entities for the connected ad
   account, level=ad_account, fields=["id","name","amount_spent"],
   time_range={"since": <60 days ago>, "until": today}, time_increment="1".
   Verbatim shape: {"ad_entities": "[{...}, ...]"} (a JSON-encoded string).

4. --orders <path> [<path> ...] (one or more, for pagination)
   Raw output(s) of mcp__Shopify__graphql_query for:
     query Orders($cursor: String) {
       orders(first: 100, sortKey: CREATED_AT, reverse: true, after: $cursor) {
         pageInfo { hasNextPage endCursor }
         edges { node { createdAt paymentGatewayNames
                         totalPriceSet { shopMoney { amount } } } }
       }
     }
   Paginate with the returned endCursor until hasNextPage is false or orders
   older than the window are reached (~60 days = roughly 3-8 pages depending
   on order volume). Pass every page's raw JSON as a separate --orders arg.

Output: writes dashboard/data.json (same shape fetch_metrics.py writes).
Run `python3 build.py` afterwards to embed it into index.html.
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_metrics import build_daily, rollup  # noqa: E402

DATA_TYPE_CASTERS = {
    "INTEGER": lambda v: int(v) if v not in (None, "") else 0,
    "MONEY": lambda v: float(v) if v not in (None, "") else 0.0,
    "PERCENT": lambda v: float(v) if v not in (None, "") else 0.0,
    "DECIMAL": lambda v: float(v) if v not in (None, "") else 0.0,
}


def parse_shopifyql(raw):
    """raw: {"columns": [{"name","dataType"}], "rows": [[...]], ...} -> list[dict] keyed by column name."""
    cols = raw["columns"]
    out = []
    for row in raw["rows"]:
        rec = {}
        for col, val in zip(cols, row):
            name = col["name"]
            caster = DATA_TYPE_CASTERS.get(col.get("dataType"))
            rec[name] = caster(val) if caster else val
        out.append(rec)
    return out


def index_by_day(records, day_key="day"):
    return {r[day_key][:10]: r for r in records}


def parse_money_string(s):
    """'₹2,240.07\xa0INR' -> 2240.07"""
    cleaned = re.sub(r"[^0-9.]", "", s)
    return float(cleaned) if cleaned else 0.0


def parse_adspend(raw):
    entities = json.loads(raw["ad_entities"])
    out = {}
    for e in entities:
        day = e["date_start"]
        out[day] = parse_money_string(e["amount_spent"])
    return out


def parse_cod_split(raw_pages, days_back):
    cutoff = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
    by_day = {}
    for raw in raw_pages:
        edges = raw["data"]["orders"]["edges"]
        for e in edges:
            node = e["node"]
            day = node["createdAt"][:10]
            if day < cutoff:
                continue
            amount = float(node["totalPriceSet"]["shopMoney"]["amount"])
            if amount <= 0:
                continue
            is_cod = "Cash on Delivery (COD)" in (node["paymentGatewayNames"] or [])
            bucket = by_day.setdefault(day, {"cod": 0, "prepaid": 0, "cod_value": 0.0, "prepaid_value": 0.0})
            if is_cod:
                bucket["cod"] += 1
                bucket["cod_value"] += amount
            else:
                bucket["prepaid"] += 1
                bucket["prepaid_value"] += amount
    return by_day


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sales", required=True)
    ap.add_argument("--sessions", required=True)
    ap.add_argument("--adspend", required=True)
    ap.add_argument("--orders", required=True, nargs="+")
    ap.add_argument("--days-back", type=int, default=60)
    ap.add_argument("--shop", default="Elan Clothing (elanclothing.in)")
    args = ap.parse_args()

    sales_raw = json.loads(Path(args.sales).read_text())
    sessions_raw = json.loads(Path(args.sessions).read_text())
    adspend_raw = json.loads(Path(args.adspend).read_text())
    orders_raw = [json.loads(Path(p).read_text()) for p in args.orders]

    sales = index_by_day(parse_shopifyql(sales_raw))
    sessions = index_by_day(parse_shopifyql(sessions_raw))
    adspend = parse_adspend(adspend_raw)
    cod_by_day = parse_cod_split(orders_raw, args.days_back)

    daily = build_daily(sales, sessions, {}, adspend, cod_by_day)
    if not daily:
        print("No daily rows produced — check input files.", file=sys.stderr)
        sys.exit(1)
    weekly, monthly = rollup(daily)

    output = {
        "meta": {
            "shop": args.shop,
            "currency": "INR",
            "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "range": {"from": daily[0]["date"], "to": daily[-1]["date"]},
            "notes": [
                "Gross Sales, Discounts, AOV and Sessions are sourced from Shopify's ShopifyQL 'sales' and 'sessions' datasets. Conversion% is computed as Orders / Sessions.",
                "Quantity uses Shopify's net_items_sold metric (units sold, net of returns) for the period.",
                "Ad Spend and ROAS input revenue come from the connected Meta Ads account.",
                "ROAS = Revenue (Gross Sales - Discounts) / Meta Ad Spend for the same period. Days with zero ad spend show ROAS as not applicable.",
                "COD vs Prepaid split is computed per day from each order's payment gateway names (Cash on Delivery (COD) vs online gateways), then rolled up into weekly/monthly averages.",
            ],
        },
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
    }

    out_path = Path(__file__).parent / "data.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"Wrote {out_path} ({len(daily)} days, {len(weekly)} weeks, {len(monthly)} months)")
    print("Run `python3 build.py` next to embed the refreshed data into index.html.")


if __name__ == "__main__":
    main()
