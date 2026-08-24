#!/usr/bin/env python3
"""
Regenerates dashboard/data.json from live Shopify + Meta Ads data.

Pulls, per day over the requested window:
  - Shopify ShopifyQL 'sales' dataset: orders, gross_sales, discounts, average_order_value
  - Shopify ShopifyQL 'sessions' dataset: sessions
  - Shopify ShopifyQL 'sales' dataset: net_items_sold (gross quantity)
  - Shopify Admin GraphQL: order payment_gateway_names, to split COD vs Prepaid
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


def fetch_cod_split(domain, token, days_back, page_limit=10):
    """Paginate recent orders and compute COD vs prepaid share by day, from
    Order.paymentGatewayNames. Stops after page_limit pages (default 1000 orders)
    or once orders older than the window are reached, whichever comes first."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
    by_day = {}  # date -> {"cod": n, "prepaid": n, "cod_value": x, "prepaid_value": x}
    cursor = None
    query = """
    query Orders($cursor: String) {
      orders(first: 100, sortKey: CREATED_AT, reverse: true, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            createdAt
            paymentGatewayNames
            totalPriceSet { shopMoney { amount } }
          }
        }
      }
    }
    """
    for _ in range(page_limit):
        data = shopify_graphql(domain, token, query, {"cursor": cursor})
        edges = data["orders"]["edges"]
        stop = False
        for e in edges:
            node = e["node"]
            day = node["createdAt"][:10]
            if day < cutoff:
                stop = True
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
        if stop or not data["orders"]["pageInfo"]["hasNextPage"]:
            break
        cursor = data["orders"]["pageInfo"]["endCursor"]
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

        cod = cod_by_day.get(day, {"cod": 0, "prepaid": 0, "cod_value": 0.0, "prepaid_value": 0.0})
        total_orders_sample = cod["cod"] + cod["prepaid"]
        total_value_sample = cod["cod_value"] + cod["prepaid_value"]
        cod_order_pct = (cod["cod"] / total_orders_sample * 100) if total_orders_sample else 0
        prepaid_order_pct = 100 - cod_order_pct if total_orders_sample else 0
        cod_value_pct = (cod["cod_value"] / total_value_sample * 100) if total_value_sample else 0
        prepaid_value_pct = 100 - cod_value_pct if total_value_sample else 0

        days.append({
            "date": day, "orders": orders, "gross_sales": round(gross_sales, 2),
            "discounts": round(discounts, 2), "revenue": round(revenue, 2), "quantity": qty,
            "sessions": sess, "conversion_rate": round(conv, 3), "aov": round(aov, 2),
            "ad_spend": round(spend, 2), "roas": round(roas, 2) if roas is not None else None,
            "discount_pct": round(discount_pct, 2),
            "cod_order_pct": round(cod_order_pct, 1), "prepaid_order_pct": round(prepaid_order_pct, 1),
            "cod_value_pct": round(cod_value_pct, 1), "prepaid_value_pct": round(prepaid_value_pct, 1),
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
    cod_orders = sum(r["orders"] * r["cod_order_pct"] / 100 for r in rows)
    cod_order_pct = (cod_orders / orders * 100) if orders else 0
    return {
        "orders": orders, "gross_sales": round(gross_sales, 2), "discounts": round(discounts, 2),
        "revenue": round(revenue, 2), "quantity": qty, "sessions": sess,
        "conversion_rate": round(conv, 3), "aov": round(aov, 2), "ad_spend": round(spend, 2),
        "roas": round(roas, 2) if roas is not None else None, "discount_pct": round(discount_pct, 2),
        "cod_order_pct": round(cod_order_pct, 1), "prepaid_order_pct": round(100 - cod_order_pct, 1),
        "cod_value_pct": round(sum(r["cod_value_pct"] for r in rows) / len(rows), 1),
        "prepaid_value_pct": round(sum(r["prepaid_value_pct"] for r in rows) / len(rows), 1),
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

    print("Fetching payment gateway split (paginated orders, may take a moment)...")
    cod_by_day = fetch_cod_split(domain, token, days_back)

    print("Fetching Meta Ads spend...")
    adspend = fetch_meta_spend(meta_account, meta_token, days_back)

    daily = build_daily(sales, sessions, {}, adspend, cod_by_day)
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
                "COD vs Prepaid split is computed per day from each order's payment gateway names (Cash on Delivery (COD) vs online gateways), then rolled up into weekly/monthly averages.",
            ],
        },
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
    }

    out_path = os.path.join(os.path.dirname(__file__), "data.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {out_path} ({len(daily)} days, {len(weekly)} weeks, {len(monthly)} months)")
    print("Run `python3 build.py` next to embed the refreshed data into index.html.")


if __name__ == "__main__":
    main()
