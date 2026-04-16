#!/usr/bin/env python3
"""
GC Listing Pattern Analyzer
Pulls ~2400 used items from Algolia and shows how GC timestamps new listings.
Run: python3 analyze_listings.py
"""
import requests
import time
from datetime import datetime
from collections import Counter

ALGOLIA_APP_ID  = "7AQ22QS8RJ"
ALGOLIA_API_KEY = "d04d765e552eb08aff3601eae8f2b729"
ALGOLIA_INDEX   = "cD-guitarcenter"
ALGOLIA_URL     = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
ALGOLIA_HEADERS = {
    "x-algolia-application-id": ALGOLIA_APP_ID,
    "x-algolia-api-key":        ALGOLIA_API_KEY,
    "Content-Type":             "application/json",
}

def fetch_page(page):
    ts = int(time.time())
    payload = {"requests": [{
        "indexName":     ALGOLIA_INDEX,
        "facetFilters":  ["categoryPageIds:Used", "condition.lvl0:Used"],
        "hitsPerPage":   240,
        "numericFilters": [f"startDate<={ts}"],
        "page":          page,
        "query":         "",
        "attributesToRetrieve": ["objectID", "startDate", "creationDate", "displayName"],
    }]}
    r = requests.post(ALGOLIA_URL, headers=ALGOLIA_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def ts_to_iso(ts_seconds):
    try:
        return datetime.utcfromtimestamp(float(ts_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except:
        return None

print("Fetching page 1 to check total pages…")
data = fetch_page(0)
result = data["results"][0]
nb_pages = result.get("nbPages", 1)
total_hits = result.get("nbHits", 0)
print(f"  Total used items in Algolia: {total_hits:,}  |  Pages: {nb_pages}")

# Fetch up to 10 pages (~2400 items) for the analysis
pages_to_fetch = min(nb_pages, 10)
all_timestamps = []

hits = result.get("hits", [])
for h in hits:
    st = h.get("startDate") or 0
    ct = h.get("creationDate") or 0
    iso = ts_to_iso(st) if st else ts_to_iso(ct / 1000 if ct else None)
    if iso:
        all_timestamps.append(iso)

for pg in range(1, pages_to_fetch):
    print(f"  Fetching page {pg+1}/{pages_to_fetch}…", end="\r")
    d = fetch_page(pg)
    for h in d["results"][0].get("hits", []):
        st = h.get("startDate") or 0
        ct = h.get("creationDate") or 0
        iso = ts_to_iso(st) if st else ts_to_iso(ct / 1000 if ct else None)
        if iso:
            all_timestamps.append(iso)

print(f"\nAnalyzing {len(all_timestamps):,} items…\n")

dates   = [t[:10]       for t in all_timestamps]
hours   = [int(t[11:13]) for t in all_timestamps]
minutes = [int(t[14:16]) for t in all_timestamps]
seconds = [int(t[17:19]) for t in all_timestamps]

on_midnight = sum(1 for t in all_timestamps if t[11:19] == "00:00:00")
on_zero_sec = sum(1 for t in all_timestamps if t[17:19] == "00")
on_zero_min = sum(1 for t in all_timestamps if t[14:19] == "00:00")
n = len(all_timestamps)

print("=" * 60)
print("TIMESTAMP PRECISION SIGNALS")
print("=" * 60)
print(f"  Exactly midnight UTC (00:00:00):  {on_midnight:>5,}  ({on_midnight/n*100:5.1f}%)")
print(f"  Top of any hour   (XX:00:00):     {on_zero_min:>5,}  ({on_zero_min/n*100:5.1f}%)")
print(f"  Zero seconds      (XX:XX:00):     {on_zero_sec:>5,}  ({on_zero_sec/n*100:5.1f}%)")
print()
print("  → If midnight % is high: GC does a nightly batch publish")
print("  → If top-of-hour % is high: GC runs an hourly job")
print("  → If zero-seconds % is high: timestamps are minute-precision (not exact)")

print()
print("=" * 60)
print("ITEMS BY HOUR OF DAY (UTC)")
print("=" * 60)
by_hour = sorted(Counter(hours).items())
max_h = max(c for _, c in by_hour)
for h, c in by_hour:
    bar = "█" * int(c / max_h * 40)
    print(f"  {h:02d}:00  {bar:<40}  {c:,}")

print()
print("=" * 60)
print("ITEMS BY MINUTE WITHIN HOUR")
print("=" * 60)
by_min = sorted(Counter(minutes).items())
max_m = max(c for _, c in by_min)
for m, c in by_min:
    bar = "█" * int(c / max_m * 40)
    spike = " ◄ SPIKE" if c > max_m * 0.5 and m != 0 else ""
    print(f"  :{m:02d}  {bar:<40}  {c:,}{spike}")

print()
print("=" * 60)
print("ITEMS BY SECOND WITHIN MINUTE (top 10 seconds)")
print("=" * 60)
by_sec = Counter(seconds).most_common(10)
for s, c in sorted(by_sec):
    bar = "█" * int(c / by_sec[0][1] * 40)
    print(f"  :{s:02d}  {bar:<40}  {c:,}")

print()
print("=" * 60)
print("ITEMS PER DATE (most recent 20 days)")
print("=" * 60)
by_date = sorted(Counter(dates).items(), reverse=True)[:20]
max_d = max(c for _, c in by_date)
for d, c in by_date:
    bar = "█" * int(c / max_d * 40)
    print(f"  {d}  {bar:<40}  {c:,}")

print()
print("=" * 60)
print("40 MOST RECENT date_listed VALUES (raw)")
print("=" * 60)
recent = sorted(all_timestamps, reverse=True)[:40]
prev = None
for t in recent:
    gap = ""
    if prev:
        try:
            dt1 = datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ")
            dt2 = datetime.strptime(prev, "%Y-%m-%dT%H:%M:%SZ")
            diff = int((dt2 - dt1).total_seconds())
            if diff < 60:
                gap = f"  (+{diff}s)"
            elif diff < 3600:
                gap = f"  (+{diff//60}m)"
            elif diff < 86400:
                gap = f"  (+{diff//3600}h {(diff%3600)//60}m)"
            else:
                gap = f"  (+{diff//86400}d)"
        except:
            pass
    print(f"  {t}{gap}")
    prev = t
