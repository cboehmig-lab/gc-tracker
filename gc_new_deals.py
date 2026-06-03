#!/usr/bin/env python3
"""
gc_new_deals.py — Find new GC inventory priced at less than half of MSRP.
Uses the same Algolia credentials as the main tracker.

Usage:
    python3 gc_new_deals.py
    python3 gc_new_deals.py --threshold 0.6   # 40%+ off instead of 50%+
    python3 gc_new_deals.py --category Guitars
"""

import os, sys, json, time, argparse
import requests

ALGOLIA_APP_ID  = os.environ.get("ALGOLIA_APP_ID", "")
ALGOLIA_API_KEY = os.environ.get("ALGOLIA_API_KEY", "")
ALGOLIA_INDEX   = "cD-guitarcenter"
ALGOLIA_URL     = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
ALGOLIA_HEADERS = {
    "x-algolia-application-id": ALGOLIA_APP_ID,
    "x-algolia-api-key":        ALGOLIA_API_KEY,
    "Content-Type":             "application/json",
}

def fetch_page(page=0, category=None):
    ts = int(time.time())
    facet_filters = ["condition.lvl0:New"]
    if category:
        facet_filters.append(f"categoryPageIds:{category}")
    payload = {"requests": [{
        "indexName":    ALGOLIA_INDEX,
        "analyticsTags": ["Did Not Search"],
        "facetFilters": facet_filters,
        "facets":       ["*"],
        "hitsPerPage":  240,
        "numericFilters": [f"startDate<={ts}"],
        "page":         page,
    }]}
    r = requests.post(ALGOLIA_URL, headers=ALGOLIA_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    result = r.json()["results"][0]
    return result["hits"], result.get("nbPages", 1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="Max price/MSRP ratio to include (default 0.5 = under 50%% of MSRP)")
    parser.add_argument("--category", type=str, default=None,
                        help="Optional category filter, e.g. 'Guitars', 'Amplifiers', 'Effects Pedals'")
    args = parser.parse_args()

    if not ALGOLIA_APP_ID or not ALGOLIA_API_KEY:
        print("ERROR: Set ALGOLIA_APP_ID and ALGOLIA_API_KEY environment variables.")
        sys.exit(1)

    print(f"Scanning GC new inventory for items under {int(args.threshold*100)}% of MSRP...")
    if args.category:
        print(f"Category filter: {args.category}")
    print()

    seen_skus = set()
    deals = []
    page = 0

    while True:
        hits, nb_pages = fetch_page(page=page, category=args.category)
        for hit in hits:
            sku = hit.get("sku") or hit.get("objectID") or ""
            if not sku or sku in seen_skus:
                continue
            seen_skus.add(sku)

            price     = hit.get("price") or 0
            list_price = hit.get("listPrice") or 0
            name      = hit.get("displayName") or hit.get("name") or ""
            brand     = hit.get("brand") or ""
            seo_url   = hit.get("seoUrl") or ""
            url       = "https://www.guitarcenter.com" + seo_url if seo_url else ""
            category  = hit.get("categoryPageIds", [""])[0] if hit.get("categoryPageIds") else ""

            try:
                price      = float(price)
                list_price = float(list_price)
            except (TypeError, ValueError):
                continue

            if price <= 0 or list_price <= 0:
                continue
            if list_price <= price:
                continue  # no discount at all

            ratio = price / list_price
            if ratio < args.threshold:
                pct_off = int((1 - ratio) * 100)
                deals.append({
                    "name":       name,
                    "brand":      brand,
                    "category":   category,
                    "price":      price,
                    "list_price": list_price,
                    "pct_off":    pct_off,
                    "url":        url,
                })

        page += 1
        print(f"  Page {page}/{nb_pages} — {len(seen_skus)} unique SKUs scanned, {len(deals)} deals so far", end="\r")
        if page >= nb_pages:
            break

    print()
    print()

    if not deals:
        print(f"No items found under {int(args.threshold*100)}% of MSRP.")
        return

    # Sort by discount % descending
    deals.sort(key=lambda d: d["pct_off"], reverse=True)

    print(f"Found {len(deals)} items under {int(args.threshold*100)}% of MSRP:\n")
    print(f"{'%OFF':<6} {'PRICE':>8}  {'MSRP':>8}  {'NAME'}")
    print("-" * 80)
    for d in deals:
        print(f"{d['pct_off']:>3}%   ${d['price']:>7.2f}  ${d['list_price']:>7.2f}  {d['name'][:55]}")
        if d["url"]:
            print(f"       {d['url']}")
        print()

    print(f"\nTotal unique SKUs scanned: {len(seen_skus)}")

if __name__ == "__main__":
    main()
