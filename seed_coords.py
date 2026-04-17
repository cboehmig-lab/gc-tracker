#!/usr/bin/env python3
"""
seed_coords.py — Generate gc_store_coords_seed.json from the GC locations CSV.

Usage:
    python3 seed_coords.py Guitar_Center_Locations_US.csv

Geocodes each store's ZIP code via api.zippopotam.us (same service the app
uses for user ZIPs), deduplicates on store name, and writes
gc_store_coords_seed.json alongside this script.

That seed file gets committed to git and the app's _build_store_coords()
loads it first, so the admin build-coords only needs to geocode the ~130
stores Gemini missed.
"""

import csv
import json
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

CSV_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("Guitar_Center_Locations_US.csv")
OUT_FILE = Path(__file__).parent / "gc_store_coords_seed.json"

if not CSV_FILE.exists():
    print(f"ERROR: {CSV_FILE} not found. Pass the CSV path as an argument.")
    sys.exit(1)

with open(CSV_FILE, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

print(f"Read {len(rows)} rows from {CSV_FILE.name}")

seen = set()
result = {}
errors = []

for row in rows:
    raw_name = row.get("Store Name", "").strip()
    # Strip "Guitar Center " prefix to match Algolia store facet names
    name = raw_name.removeprefix("Guitar Center ").strip()
    zip_code = row.get("Zip Code", "").strip().zfill(5)
    city  = row.get("City", "").strip()
    state = row.get("State", "").strip()

    if not name or not zip_code:
        continue
    if name in seen:
        continue  # skip duplicates (Gemini listed several stores twice)
    seen.add(name)

    try:
        r = requests.get(
            f"https://api.zippopotam.us/us/{zip_code}",
            timeout=8,
            headers={"User-Agent": "GCTrackerSeedScript/1.0"},
        )
        if r.ok:
            places = r.json().get("places", [])
            if places:
                lat = float(places[0]["latitude"])
                lng = float(places[0]["longitude"])
                result[name] = {
                    "lat": lat,
                    "lng": lng,
                    "source": f"csv-zip:{zip_code} ({city}, {state})",
                }
                print(f"  ✓ {name:35s} {lat:.4f}, {lng:.4f}  [{zip_code}]")
            else:
                errors.append(f"{name}: no places in ZIP response")
                print(f"  ✗ {name}: no places for ZIP {zip_code}")
        else:
            errors.append(f"{name}: HTTP {r.status_code} for ZIP {zip_code}")
            print(f"  ✗ {name}: HTTP {r.status_code}")
    except Exception as e:
        errors.append(f"{name}: {e}")
        print(f"  ✗ {name}: {e}")

    time.sleep(0.15)  # be polite to the free API

OUT_FILE.write_text(json.dumps(result, indent=2))
print(f"\n✓ Wrote {len(result)} entries to {OUT_FILE}")
if errors:
    print(f"  {len(errors)} failures:")
    for e in errors:
        print(f"    - {e}")
print("\nNext step: commit gc_store_coords_seed.json to git and push.")
print("Then run /admin/build-coords (Force checked) to fill in the remaining stores.")
