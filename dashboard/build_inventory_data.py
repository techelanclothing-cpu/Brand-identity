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


# Products need this much evidence before their own size curve is trusted at all.
CURVE_MIN_UNITS = 20
CURVE_MIN_SIZES = 3
# Shrinkage constant: with n units of evidence a product's own curve carries
# weight n/(n+SHRINK_K), the rest coming from its category's baseline curve.
SHRINK_K = 25


def size_system(size):
    return "numeric" if (size or "").strip().isdigit() else "alpha"


def build_curves(products, sold, days):
    """Size demand curves, corrected for stock-out censoring.

    A size that sells out stops selling, so raw units understate its demand.
    Stock is walked backwards from today's level to work out how many days each
    size was actually available, and demand is measured per available day.

    The walk cannot see restocks, so it overstates how long a size was in stock,
    which understates its demand rate. The correction is therefore conservative:
    it never inflates a size beyond what the data supports.
    """
    vp, pp = "gid://shopify/ProductVariant/", "gid://shopify/Product/"
    vinfo = {}
    for p in products:
        for v in p["variants"]["nodes"]:
            vinfo[v["id"].replace(vp, "")] = {
                "pid": p["id"].replace(pp, ""), "ptitle": p["title"],
                "size": v["title"], "stock": v.get("inventoryQuantity") or 0,
            }

    for vid, info in vinfo.items():
        series = sold.get(vid, {})
        running, avail = info["stock"], 0
        for d in reversed(days):
            start = running + series.get(d, 0)
            if start > 0:
                avail += 1
            running = start
        info["units"] = sum(max(v, 0) for v in series.values())
        info["avail_days"] = avail
        info["oos_days"] = len(days) - avail
        info["rate"] = info["units"] / avail if avail else 0.0

    def norm(d):
        t = sum(d.values())
        return {k: v / t for k, v in d.items()} if t > 0 else {}

    baseline = {}
    for sysname in ("alpha", "numeric"):
        agg = defaultdict(float)
        for info in vinfo.values():
            if size_system(info["size"]) == sysname:
                agg[info["size"]] += info["rate"]
        baseline[sysname] = norm(dict(agg))

    by_product = defaultdict(list)
    for info in vinfo.values():
        by_product[info["pid"]].append(info)

    out = []
    for pid, vs in by_product.items():
        units = sum(v["units"] for v in vs)
        if units < CURVE_MIN_UNITS or len(vs) < CURVE_MIN_SIZES:
            continue
        adj = norm({v["size"]: v["rate"] for v in vs})
        if not adj:
            continue
        raw = norm({v["size"]: v["units"] for v in vs})
        sysname = size_system(vs[0]["size"])
        base = baseline[sysname]
        w = units / (units + SHRINK_K)
        sizes = sorted({v["size"] for v in vs}, key=size_sort_key)
        mix = norm({s: w * adj.get(s, 0) + (1 - w) * base.get(s, 1.0 / len(sizes)) for s in sizes})
        skew = 0.5 * sum(abs(mix.get(s, 0) - base.get(s, 0)) for s in set(mix) | set(base))
        vmap = {v["size"]: v for v in vs}
        out.append({
            "id": pid, "title": vs[0]["ptitle"], "system": sysname,
            "units": units, "confidence": round(w, 3), "skew": round(skew, 4),
            "lost": round(sum(v["oos_days"] * v["rate"] for v in vs), 1),
            "sizes": [{
                "size": s, "stock": vmap[s]["stock"], "units": vmap[s]["units"],
                "oos_days": vmap[s]["oos_days"],
                "raw": round(raw.get(s, 0), 4), "mix": round(mix.get(s, 0), 4),
            } for s in sizes],
        })

    out.sort(key=lambda r: -r["units"])
    return {
        "baseline": {k: {s: round(v, 4) for s, v in c.items()} for k, c in baseline.items()},
        "window": {"from": days[0], "to": days[-1], "days": len(days)},
        "min_units": CURVE_MIN_UNITS,
        "products": out,
    }


def build(products, sales_rows, daily_totals, end_date):
    end = datetime.date.fromisoformat(end_date)
    win = {
        "d1": [(end - datetime.timedelta(days=i)).isoformat() for i in range(1)],
        "d3": [(end - datetime.timedelta(days=i)).isoformat() for i in range(3)],
        "d7": [(end - datetime.timedelta(days=i)).isoformat() for i in range(7)],
    }

    # Gross sales and returns per window, kept apart from the net figure. A day
    # when a batch of returns is processed can net out near zero while the shop
    # sold normally, and a net-only run rate reads as a demand collapse.
    flow = {k: {"gross": 0, "returns": 0} for k in ("d1", "d3", "d7")}
    # variant id -> units sold within each window
    sold = defaultdict(lambda: {"d1": 0, "d3": 0, "d7": 0})
    # variant id -> {day: units}, over the whole pulled window, for the curves
    series = defaultdict(dict)
    all_days = set()
    for r in sales_rows:
        vid = r.get("product_variant_id")
        day = r["day"][:10]
        all_days.add(day)
        if not vid:
            continue  # rows with a null variant id are unattributed adjustments
        qty = int(r["net_items_sold"])
        series[vid][day] = series[vid].get(day, 0) + qty
        for k, days in win.items():
            if day in days:
                sold[vid][k] += qty
                flow[k]["gross" if qty > 0 else "returns"] += qty
    curve_days = sorted(d for d in all_days if d <= end_date)

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
        "gross": {k: flow[k]["gross"] for k in flow},
        "returns": {k: -flow[k]["returns"] for k in flow},
    }

    curves = build_curves(products, series, curve_days) if curve_days else None

    return {
        "curves": curves,
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
    c = data.get("curves")
    if c:
        print(f"  size curves: {len(c['products'])} products over {c['window']['days']} days "
              f"({c['window']['from']} to {c['window']['to']})")


if __name__ == "__main__":
    main()
