#!/usr/bin/env python3
"""
Regenerates dashboard/data.json from live Shopify + Meta Ads data.

Pulls, per day over the requested window:
  - Shopify ShopifyQL 'sales' dataset: orders, gross_sales, discounts, average_order_value
  - Shopify ShopifyQL 'sessions' dataset: sessions
  - Shopify ShopifyQL 'sales' dataset: net_items_sold (gross quantity)
  - Shopify Admin GraphQL ordersCount: exact per-day order counts by financial
    status, to split COD vs Prepaid (Paid = Prepaid, Partially Paid = COD —
    the store collects a token amount online and the balance on delivery)
  - Meta Marketing API insights: amount_spent (ad spend) for the connected ad account

Then rolls the daily rows up into weekly (ISO week) and monthly buckets, computes
derived metrics (Revenue, ROAS, Discount%, Conversion%), and writes dashboard/data.json
in the same shape index.html expects.

Environment variables required:
  SHOPIFY_STORE_DOMAIN     e.g. "elan-clothing-r.myshopify.com"
  SHOPIFY_CLIENT_ID        Client ID of a Dev Dashboard app installed on the store,
                           with read_orders + read_analytics scopes
  SHOPIFY_CLIENT_SECRET    Client secret for that same app
  META_AD_ACCOUNT_ID       numeric ad account id, e.g. "1778418969517591"
  META_ACCESS_TOKEN        Meta Marketing API access token with ads_read scope

Shopify auth uses the client credentials grant (the current way to authenticate a
server-side app against your own store — admin-created "legacy custom apps" with a
static access token can no longer be created as of Jan 1, 2026). This script
exchanges SHOPIFY_CLIENT_ID/SECRET for a short-lived (24h) access token at the start
of each run — see get_shopify_access_token() below. Create the app once in the
Shopify Dev Dashboard (dev.shopify.com/dashboard), give it read_orders +
read_analytics, install it on the store, and use its Client ID/secret here — see
dashboard/README.md for the full walkthrough.

Optional:
  DAYS_BACK                how many days of history to pull (default 60)

Usage:
  python3 fetch_metrics.py
  python3 build.py   # re-embeds the refreshed data.json into index.html
"""
import os
import sys
import json
import datetime
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

SHOPIFY_API_VERSION = "2025-01"


def env(name, required=True, default=None):
    v = os.environ.get(name, default)
    if required and not v:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return v


def get_shopify_access_token(domain, client_id, client_secret):
    """Client credentials grant: exchange the app's own credentials for a 24h
    access token, scoped to whatever access scopes the app's Dev Dashboard
    version has released. No merchant/OAuth redirect involved."""
    url = f"https://{domain}/admin/oauth/access_token"
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Shopify token exchange failed ({e.code}): {detail}\n"
            "Check SHOPIFY_CLIENT_ID/SHOPIFY_CLIENT_SECRET, and that the app "
            "is installed on this store and in the same Shopify organization."
        ) from e
    return payload["access_token"]


def shopify_graphql(domain, token, query, variables=None):
    url = f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Shopify-Access-Token", token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if "errors" in payload:
        raise RuntimeError(f"Shopify GraphQL error: {payload['errors']}")
    return payload["data"]


def shopifyql(domain, token, query):
    gql = """
    query ShopifyQL($query: String!) {
      shopifyqlQuery(query: $query) {
        ... on TableData {
          tableData {
            columns { name dataType }
            rowData
          }
        }
        parseErrors { code message }
      }
    }
    """
    data = shopify_graphql(domain, token, gql, {"query": query})
    result = data["shopifyqlQuery"]
    if result.get("parseErrors"):
        raise RuntimeError(f"ShopifyQL error for query {query!r}: {result['parseErrors']}")
    table = result["tableData"]
    cols = [c["name"] for c in table["columns"]]
    rows = table["rowData"]
    return [dict(zip(cols, row)) for row in rows]


def fetch_sales(domain, token, days_back):
    rows = shopifyql(
        domain, token,
        f"FROM sales SHOW orders, gross_sales, discounts, total_sales, average_order_value, net_items_sold "
        f"TIMESERIES day SINCE -{days_back}d UNTIL today",
    )
    return {r["day"][:10]: r for r in rows}


def fetch_sessions(domain, token, days_back):
    rows = shopifyql(
        domain, token,
        f"FROM sessions SHOW sessions TIMESERIES day SINCE -{days_back}d UNTIL today",
    )
    return {r["day"][:10]: r for r in rows}


def fetch_cod_split(domain, token, days_back):
    """Per-day COD vs Prepaid order counts, classified from Shopify's own
    financial status (per the store owner: Paid = Prepaid, Partially Paid =
    COD — a token amount is collected online at checkout and the balance is
    paid on delivery). Uses one GraphQL request with an aliased ordersCount
    field per day/status instead of paginating raw orders, which is both
    faster and exact (no per-page sampling)."""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days_back - 1)
    by_day = {}
    days = [start + datetime.timedelta(days=i) for i in range((today - start).days + 1)]
    for chunk_start in range(0, len(days), 15):
        chunk = days[chunk_start:chunk_start + 15]
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
        data = shopify_graphql(domain, token, "query {\n  " + "\n  ".join(parts) + "\n}")
        for d in chunk:
            key = d.isoformat().replace("-", "_")
            paid = data[f"p_{key}"]["count"]
            partially_paid = data[f"pp_{key}"]["count"]
            by_day[d.isoformat()] = {"cod": partially_paid, "prepaid": paid}
    return by_day


def fetch_meta_spend(ad_account_id, token, days_back):
    since = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
    until = datetime.date.today().isoformat()
    params = {
        "fields": "spend",
        "time_range": json.dumps({"since": since, "until": until}),
        "time_increment": "1",
        "access_token": token,
        "limit": "500",
    }
    url = f"https://graph.facebook.com/v19.0/act_{ad_account_id}/insights?" + urllib.parse.urlencode(params)
    out = {}
    while url:
        with urllib.request.urlopen(url, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        for row in payload.get("data", []):
            out[row["date_start"]] = float(row.get("spend", 0) or 0)
        url = payload.get("paging", {}).get("next")
    return out


def build_daily(sales, sessions, items_by_day, adspend, cod_by_day):
    days = []
    for day in sorted(sales.keys()):
        s = sales[day]
        orders = int(s["orders"])
        gross_sales = float(s["gross_sales"])
        discounts = abs(float(s["discounts"]))
        aov = float(s["average_order_value"])
        sess = int(sessions.get(day, {}).get("sessions", 0))
        qty = int(s.get("net_items_sold", 0) or 0)
        spend = float(adspend.get(day, 0.0))

        revenue = gross_sales - discounts
        discount_pct = (discounts / gross_sales * 100) if gross_sales else 0
        conv = (orders / sess * 100) if sess else 0
        roas = (revenue / spend) if spend else None

        cod = cod_by_day.get(day, {"cod": 0, "prepaid": 0})
        classified = cod["cod"] + cod["prepaid"]
        cod_order_pct = (cod["cod"] / classified * 100) if classified else 0
        prepaid_order_pct = (cod["prepaid"] / classified * 100) if classified else 0

        days.append({
            "date": day, "orders": orders, "gross_sales": round(gross_sales, 2),
            "discounts": round(discounts, 2), "revenue": round(revenue, 2), "quantity": qty,
            "sessions": sess, "conversion_rate": round(conv, 3), "aov": round(aov, 2),
            "ad_spend": round(spend, 2), "roas": round(roas, 2) if roas is not None else None,
            "discount_pct": round(discount_pct, 2),
            "cod_orders": cod["cod"], "prepaid_orders": cod["prepaid"],
            "cod_order_pct": round(cod_order_pct, 1), "prepaid_order_pct": round(prepaid_order_pct, 1),
        })
    return days


def agg(rows):
    orders = sum(r["orders"] for r in rows)
    gross_sales = sum(r["gross_sales"] for r in rows)
    discounts = sum(r["discounts"] for r in rows)
    revenue = sum(r["revenue"] for r in rows)
    qty = sum(r["quantity"] for r in rows)
    sess = sum(r["sessions"] for r in rows)
    spend = sum(r["ad_spend"] for r in rows)
    aov = (gross_sales / orders) if orders else 0
    conv = (orders / sess * 100) if sess else 0
    discount_pct = (discounts / gross_sales * 100) if gross_sales else 0
    roas = (revenue / spend) if spend else None
    cod_orders = sum(r["cod_orders"] for r in rows)
    prepaid_orders = sum(r["prepaid_orders"] for r in rows)
    classified = cod_orders + prepaid_orders
    cod_order_pct = (cod_orders / classified * 100) if classified else 0
    prepaid_order_pct = (prepaid_orders / classified * 100) if classified else 0
    return {
        "orders": orders, "gross_sales": round(gross_sales, 2), "discounts": round(discounts, 2),
        "revenue": round(revenue, 2), "quantity": qty, "sessions": sess,
        "conversion_rate": round(conv, 3), "aov": round(aov, 2), "ad_spend": round(spend, 2),
        "roas": round(roas, 2) if roas is not None else None, "discount_pct": round(discount_pct, 2),
        "cod_orders": cod_orders, "prepaid_orders": prepaid_orders,
        "cod_order_pct": round(cod_order_pct, 1), "prepaid_order_pct": round(prepaid_order_pct, 1),
    }


def rollup(days):
    weekly = {}
    monthly = {}
    for r in days:
        d = datetime.date.fromisoformat(r["date"])
        iso = d.isocalendar()
        wk = f"{iso[0]}-W{iso[1]:02d}"
        weekly.setdefault(wk, []).append(r)
        mk = r["date"][:7]
        monthly.setdefault(mk, []).append(r)

    weekly_out = []
    for key in sorted(weekly.keys()):
        rows = weekly[key]
        dates = sorted(x["date"] for x in rows)
        entry = agg(rows)
        entry.update({"period": key, "label": f"{dates[0]} to {dates[-1]}", "days_in_period": len(rows)})
        weekly_out.append(entry)

    monthly_out = []
    for key in sorted(monthly.keys()):
        rows = monthly[key]
        entry = agg(rows)
        entry.update({"period": key, "label": key, "days_in_period": len(rows)})
        monthly_out.append(entry)

    return weekly_out, monthly_out


def merge_daily(existing_daily, fresh_daily):
    """Merge a freshly-pulled window of daily rows into the full archive.

    Rows for dates covered by the fresh pull replace whatever was there
    before (Shopify orders can be refunded/edited after the fact, so the
    trailing window is always re-synced); rows for older dates that the
    fresh pull didn't touch are kept as-is, so the archive only grows."""
    by_date = {r["date"]: r for r in existing_daily}
    for r in fresh_daily:
        by_date[r["date"]] = r
    return [by_date[d] for d in sorted(by_date.keys())]


def load_existing_daily(data_json_path):
    if not data_json_path.exists():
        return []
    try:
        existing = json.loads(data_json_path.read_text())
        return existing.get("daily", [])
    except (json.JSONDecodeError, OSError):
        return []


def main():
    domain = env("SHOPIFY_STORE_DOMAIN")
    client_id = env("SHOPIFY_CLIENT_ID")
    client_secret = env("SHOPIFY_CLIENT_SECRET")
    meta_account = env("META_AD_ACCOUNT_ID")
    meta_token = env("META_ACCESS_TOKEN")

    print("Exchanging Shopify client credentials for an access token...")
    token = get_shopify_access_token(domain, client_id, client_secret)
    days_back = int(os.environ.get("DAYS_BACK", "60"))

    print(f"Fetching {days_back} days of Shopify sales/sessions data...")
    sales = fetch_sales(domain, token, days_back)
    sessions = fetch_sessions(domain, token, days_back)

    print("Fetching COD/Prepaid split by financial status...")
    cod_by_day = fetch_cod_split(domain, token, days_back)

    print("Fetching Meta Ads spend...")
    adspend = fetch_meta_spend(meta_account, meta_token, days_back)

    fresh_daily = build_daily(sales, sessions, {}, adspend, cod_by_day)

    out_path = Path(os.path.dirname(__file__) or ".") / "data.json"
    existing_daily = load_existing_daily(out_path)
    daily = merge_daily(existing_daily, fresh_daily)
    weekly, monthly = rollup(daily)

    output = {
        "meta": {
            "shop": domain,
            "currency": "INR",
            "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "range": {"from": daily[0]["date"], "to": daily[-1]["date"]} if daily else {},
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

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    new_days = len(daily) - len(existing_daily)
    print(
        f"Wrote {out_path}: {len(daily)} days total in archive "
        f"({new_days:+d} vs previous run), {len(weekly)} weeks, {len(monthly)} months"
    )
    print("Run `python3 build.py` next to embed the refreshed data into index.html.")


if __name__ == "__main__":
    main()
