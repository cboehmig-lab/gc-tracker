#\!/usr/bin/env python3
"""Quick probe: does Algolia include _geoloc or any lat/lng on used-item hits?"""
import requests, json

r = requests.post(
    "https://7aq22qs8rj-dsn.algolia.net/1/indexes/*/queries",
    headers={
        "x-algolia-application-id": "7AQ22QS8RJ",
        "x-algolia-api-key": "d04d765e552eb08aff3601eae8f2b729",
        "Content-Type": "application/json",
    },
    json={"requests": [{
        "indexName": "cD-guitarcenter",
        "facetFilters": ["categoryPageIds:Used", "condition.lvl0:Used"],
        "hitsPerPage": 5, "page": 0, "query": "",
        "attributesToRetrieve": ["*"]
    }]},
    timeout=20
)
hits = r.json()["results"][0]["hits"]
for hit in hits:
    geo_fields = {k: v for k, v in hit.items()
                  if any(x in k.lower() for x in ["geo", "lat", "lng", "lon", "coord", "store"])}
    print(json.dumps(geo_fields, indent=2))
    print("---")
