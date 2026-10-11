"""City/state search for re-pinning the demo facility, via OpenStreetMap Nominatim (US only).

Nominatim's usage policy asks for an identifying User-Agent and at most one request per second;
this is only called when a person searches, so that is easily met.
"""
from dataclasses import dataclass

from .http import get_json

NOMINATIM = "https://nominatim.openstreetmap.org/search"


@dataclass
class Place:
    label: str
    lat: float
    lon: float


def search(query: str, limit: int = 6) -> list[Place]:
    f = get_json("osm", NOMINATIM, params={"q": query, "countrycodes": "us", "format": "jsonv2",
                                          "limit": limit, "addressdetails": 1})
    out = []
    for r in f.body:
        a = r.get("address") or {}
        town = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("county") or r["name"]
        state = a.get("state", "")
        county = a.get("county", "")
        label = ", ".join(x for x in (town, county if county != town else "", state) if x)
        out.append(Place(label=label, lat=round(float(r["lat"]), 4), lon=round(float(r["lon"]), 4)))
    return out
