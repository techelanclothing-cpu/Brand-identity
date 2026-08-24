#!/usr/bin/env python3
"""
Regenerates dashboard/data.json from the *raw JSON responses* of the Shopify
and Meta Ads MCP tools available inside a Claude session (Shopify connector,
Meta Ads connector) — no API tokens required, unlike fetch_metrics.py which
talks to the raw HTTP APIs directly.

This is the script the scheduled "refresh the dashboard" Routine uses: a
Claude session calls the MCP tools listed below, dumps each raw result to a
JSON file, then runs this script to turn those dumps into data.json.

Two subcommands:

  gen-cod-query   Prints the GraphQL query text(s) needed for the COD/Prepaid
                  split, chunked into batches of 15 days each (Shopify's
                  ordersCount is called once per day per financial status via
                  aliases, so one call covers many days at once — a 60-day
                  window needs only ~4 chunks/calls, not one per order page).

  build           Builds data.json from the raw tool outputs.

--- gen-cod-query ---

  python3 refresh_from_mcp.py gen-cod-query --days-back 60

Prints N query strings, one per chunk, separated by a line of "---". Run each
through mcp__Shopify__graphql_query and save each raw JSON result to its own
file (see "COD/Prepaid classification" below for what the fields mean).

--- build ---

Required input files (paths passed as CLI args):

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

4. --cod-counts <path> [<path> ...] (one per chunk from gen-cod-query)
   Raw output(s) of mcp__Shopify__graphql_query for each query chunk printed
   by `gen-cod-query`. Verbatim shape:
     {"data": {"p_2026_08_24": {"count": 13}, "pp_2026_08_24": {"count": 3}, ...}}

COD/Prepaid classification: per the store owner, Shopify's order financial
status maps directly to how the order was paid — "Paid" (p_ alias) means
Prepaid (paid in full online at checkout), "Partially Paid" (pp_ alias) means
COD (a token amount collected online, balance due on delivery). Other
statuses (pending, refunded, voided, ...) aren't counted in the split.

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
from fetch_metrics import build_daily, rollup, merge_daily, load_existing_daily  # noqa: E402

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


def cod_query_chunks(days_back, chunk_size=15):
    """Yield GraphQL query strings, each covering up to chunk_size days of
    aliased ordersCount(financial_status:paid) / ordersCount(financial_status:
    partially_paid) fields. See module docstring for why this beats paginating
    raw orders: one request classifies many days at once."""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days_back - 1)
    days = [start + datetime.timedelta(days=i) for i in range((today - start).days + 1)]
    for i in range(0, len(days), chunk_size):
        chunk = days[i:i + chunk_size]
        parts = []
        for d in chunk:
            d0, d1 = d.isoformat(), (d + datetime.timedelta(days=1)).isoformat()
            key = d.isoformat().replace("-", "_")
            parts.append(
                f'p_{key}: ordersCount(query: "created_at:>=\'{d0}T00:00:00Z\' '
                f"created_at:<'{d1}T00:00:00Z' financial_status:paid\") {{ count }}"
            )
            parts.append(
                f'pp_{key}: ordersCount(query: "created_at:>=\'{d0}T00:00:00Z\' '
                f"created_at:<'{d1}T00:00:00Z' financial_status:partially_paid\") {{ count }}"
            )
        yield "query {\n  " + "\n  ".join(parts) + "\n}"


def parse_cod_counts(raw_chunks):
    """raw_chunks: list of {"data": {"p_YYYY_MM_DD": {"count": n}, "pp_YYYY_MM_DD": {"count": n}, ...}}
    -> {"YYYY-MM-DD": {"cod": n, "prepaid": n}}"""
    by_day = {}
    for raw in raw_chunks:
        data = raw["data"]
        for alias, val in data.items():
            prefix, datekey = alias.split("_", 1)
            day = datekey.replace("_", "-")
            bucket = by_day.setdefault(day, {"cod": 0, "prepaid": 0})
            if prefix == "p":
                bucket["prepaid"] = val["count"]
            elif prefix == "pp":
                bucket["cod"] = val["count"]
    return by_day


def cmd_gen_cod_query(args):
    chunks = list(cod_query_chunks(args.days_back, args.chunk_days))
    print(f"\n--- {len(chunks)} queries, ~{args.chunk_days} days each ---\n", file=sys.stderr)
    print(("\n" + "-" * 3 + "\n").join(chunks))


def cmd_build(args):
    sales_raw = json.loads(Path(args.sales).read_text())
    sessions_raw = json.loads(Path(args.sessions).read_text())
    adspend_raw = json.loads(Path(args.adspend).read_text())
    cod_raw = [json.loads(Path(p).read_text()) for p in args.cod_counts]

    sales = index_by_day(parse_shopifyql(sales_raw))
    sessions = index_by_day(parse_shopifyql(sessions_raw))
    adspend = parse_adspend(adspend_raw)
    cod_by_day = parse_cod_counts(cod_raw)

    fresh_daily = build_daily(sales, sessions, {}, adspend, cod_by_day)
    if not fresh_daily:
        print("No daily rows produced — check input files.", file=sys.stderr)
        sys.exit(1)

    out_path = Path(__file__).parent / "data.json"
    existing_daily = load_existing_daily(out_path)
    daily = merge_daily(existing_daily, fresh_daily)
    weekly, monthly = rollup(daily)

    days_back = len(fresh_daily)
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
                "COD vs Prepaid is classified per order from Shopify's financial status: Paid = Prepaid, Partially Paid = COD (a token amount collected online, balance on delivery). Orders in other states (pending, refunded, voided) aren't counted in the split. Computed per day from exact order counts, then rolled up into weekly/monthly totals.",
                f"This is a growing archive, not a rolling window: each refresh re-syncs the last {days_back} days (to catch refunds/edits to recent orders) and keeps every earlier day untouched, so history accumulates for as long as the dashboard has been refreshed.",
            ],
        },
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
    }

    out_path.write_text(json.dumps(output, indent=2))
    new_days = len(daily) - len(existing_daily)
    print(
        f"Wrote {out_path}: {len(daily)} days total in archive "
        f"({new_days:+d} vs previous run), {len(weekly)} weeks, {len(monthly)} months"
    )
    print("Run `python3 build.py` next to embed the refreshed data into index.html.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    gq = sub.add_parser("gen-cod-query", help="Print the ordersCount query chunks for the COD/Prepaid split")
    gq.add_argument("--days-back", type=int, default=60)
    gq.add_argument("--chunk-days", type=int, default=15)
    gq.set_defaults(func=cmd_gen_cod_query)

    bd = sub.add_parser("build", help="Build data.json from the raw MCP tool outputs")
    bd.add_argument("--sales", required=True)
    bd.add_argument("--sessions", required=True)
    bd.add_argument("--adspend", required=True)
    bd.add_argument("--cod-counts", required=True, nargs="+")
    bd.add_argument("--shop", default="Elan Clothing (elanclothing.in)")
    bd.set_defaults(func=cmd_build)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
