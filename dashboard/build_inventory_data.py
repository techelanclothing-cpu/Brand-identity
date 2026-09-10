#!/usr/bin/env python3
"""
Builds dashboard/inventory_data.json from raw Shopify pulls.

Inputs (produced by fetch_inventory.py, or by the MCP-based pull used to seed
the first snapshot):
  products_raw.json  - list of Product nodes (id/title/status/productType/
                       totalInventory/options/variants{id,title,inventoryQuantity,price})
  sales_rows.json    - list of {day, product_variant_id, net_items_sold} rows from
                       ShopifyQL `FROM sales SHOW net_items_sold GROUP BY
                       product_variant_id TIMESERIES day`
  daily_totals.json  - list of {date, units, orders} store-level rows

Run-rate windows end on the last COMPLETE day (today is excluded, since a
part-day would drag every rate down).
"""
import os
import sys
import json
import datetime
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))

# Size display order: alpha sizes first (smallest to largest), then numeric
# waist sizes, then anything unrecognised.
ALPHA_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "2XL", "XXL", "3XL", "XXXL", "4XL"]


def size_sort_key(size):
    s = (size or "").strip().upper()
    if s in ALPHA_ORDER:
        return (0, ALPHA_ORDER.index(s), s)
    if s.isdigit():
        return (1, int(s), s)
    return (2, 0, s)


def build(products, sales_rows, daily_totals, end_date):
    end = datetime.date.fromisoformat(end_date)
    win = {
        "d1": [(end - datetime.timedelta(days=i)).isoformat() for i in range(1)],
        "d3": [(end - datetime.timedelta(days=i)).isoformat() for i in range(3)],
        "d7": [(end - datetime.timedelta(days=i)).isoformat() for i in range(7)],
    }

    # variant id -> units sold within each window
    sold = defaultdict(lambda: {"d1": 0, "d3": 0, "d7": 0})
    for r in sales_rows:
        vid = r.get("product_variant_id")
        if not vid:
            continue  # rows with a null variant id are unattributed adjustments
        day = r["day"][:10]
        qty = int(r["net_items_sold"])
        for k, days in win.items():
            if day in days:
                sold[vid][k] += qty

    def rate(units, days):
        return round(units / days, 3)

    def cover(stock, rr):
        # Days of stock remaining at the given daily run rate.
        if rr <= 0:
            return None
        return round(stock / rr, 1)

    out_products = []
    size_agg = defaultdict(lambda: {"variants": 0, "stock": 0, "d1": 0, "d3": 0, "d7": 0, "oos": 0})

    for p in products:
        vid_prefix = "gid://shopify/ProductVariant/"
        variants = []
        for v in p["variants"]["nodes"]:
            raw_id = v["id"].replace(vid_prefix, "")
            s = sold.get(raw_id, {"d1": 0, "d3": 0, "d7": 0})
            qty = v.get("inventoryQuantity") or 0
            price = float(v.get("price") or 0)
            size = v["title"]
            variants.append({
                "size": size,
                "qty": qty,
                "price": price,
                "d1": s["d1"], "d3": s["d3"], "d7": s["d7"],
                "rr7": rate(s["d7"], 7),
                "cover": cover(qty, s["d7"] / 7),
            })
            a = size_agg[size]
            a["variants"] += 1
            a["stock"] += max(qty, 0)
            a["d1"] += s["d1"]; a["d3"] += s["d3"]; a["d7"] += s["d7"]
            if qty <= 0:
                a["oos"] += 1

        variants.sort(key=lambda x: size_sort_key(x["size"]))
        stock = sum(max(v["qty"], 0) for v in variants)
        d1 = sum(v["d1"] for v in variants)
        d3 = sum(v["d3"] for v in variants)
        d7 = sum(v["d7"] for v in variants)
        value = sum(max(v["qty"], 0) * v["price"] for v in variants)

        out_products.append({
            "id": p["id"].replace("gid://shopify/Product/", ""),
            "title": p["title"],
            "status": p["status"],
            "type": p.get("productType") or "Uncategorised",
            "stock": stock,
            "value": round(value, 2),
            "sizes_in_stock": sum(1 for v in variants if v["qty"] > 0),
            "sizes_total": len(variants),
            "d1": d1, "d3": d3, "d7": d7,
            "rr1": rate(d1, 1), "rr3": rate(d3, 3), "rr7": rate(d7, 7),
            "cover": cover(stock, d7 / 7),
            "variants": variants,
        })

    out_products.sort(key=lambda x: (-x["d7"], -x["stock"], x["title"]))

    sizes = []
    for size, a in sorted(size_agg.items(), key=lambda kv: size_sort_key(kv[0])):
        sizes.append({
            "size": size,
            "variants": a["variants"],
            "stock": a["stock"],
            "oos": a["oos"],
            "d1": a["d1"], "d3": a["d3"], "d7": a["d7"],
            "rr1": rate(a["d1"], 1), "rr3": rate(a["d3"], 3), "rr7": rate(a["d7"], 7),
            "cover": cover(a["stock"], a["d7"] / 7),
        })

    total_stock = sum(p["stock"] for p in out_products)
    total_d1 = sum(p["d1"] for p in out_products)
    total_d3 = sum(p["d3"] for p in out_products)
    total_d7 = sum(p["d7"] for p in out_products)
    in_stock_variants = sum(1 for p in out_products for v in p["variants"] if v["qty"] > 0)
    total_variants = sum(len(p["variants"]) for p in out_products)

    totals = {
        "products": len(out_products),
        "products_active": sum(1 for p in out_products if p["status"] == "ACTIVE"),
        "products_in_stock": sum(1 for p in out_products if p["stock"] > 0),
        "variants": total_variants,
        "variants_in_stock": in_stock_variants,
        "variants_oos": total_variants - in_stock_variants,
        "units_in_stock": total_stock,
        "retail_value": round(sum(p["value"] for p in out_products), 2),
        "d1": total_d1, "d3": total_d3, "d7": total_d7,
        "rr1": rate(total_d1, 1), "rr3": rate(total_d3, 3), "rr7": rate(total_d7, 7),
        "cover": cover(total_stock, total_d7 / 7),
    }

    return {
        "meta": {
            "shop": "Elan Clothing (elanclothing.in)",
            "currency": "INR",
            "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "inventory_as_of": datetime.date.today().isoformat(),
            "run_rate_end": end_date,
            "windows": {"d1": win["d1"][-1], "d3": win["d3"][-1], "d7": win["d7"][-1]},
        },
        "totals": totals,
        "sizes": sizes,
        "daily": daily_totals,
        "products": out_products,
    }


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else HERE
    with open(os.path.join(src, "products_raw.json")) as f:
        products = json.load(f)
    with open(os.path.join(src, "sales_rows.json")) as f:
        sales_rows = json.load(f)
    with open(os.path.join(src, "daily_totals.json")) as f:
        daily_totals = json.load(f)
    end_date = os.environ.get("RUN_RATE_END", "2026-09-09")

    data = build(products, sales_rows, daily_totals, end_date)
    out = os.path.join(HERE, "inventory_data.json")
    with open(out, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    t = data["totals"]
    print(f"Wrote {out}")
    print(f"  {t['products']} products / {t['variants']} size variants")
    print(f"  {t['units_in_stock']} units in stock, {t['variants_in_stock']} sizes in stock, {t['variants_oos']} out of stock")
    print(f"  run rate: {t['rr1']}/day (1d), {t['rr3']}/day (3d), {t['rr7']}/day (7d)")
    print(f"  days of cover at 7d rate: {t['cover']}")


if __name__ == "__main__":
    main()
