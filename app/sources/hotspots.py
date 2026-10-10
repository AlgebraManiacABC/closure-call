"""Finds places with live alerts for each hazard, so the demo facility can be re-pinned to
wherever real weather is happening right now."""
from dataclasses import dataclass

from . import nws

SEVERITY_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1}
# Warnings beat watches beat advisories.
EVENT_RANK = {"Warning": 3, "Watch": 2, "Advisory": 1}


@dataclass
class Hotspot:
    hazard: str
    event: str
    severity: str
    area_desc: str
    sender_name: str
    lat: float
    lon: float


def _rank(props: dict) -> tuple:
    kind = next((v for k, v in EVENT_RANK.items() if props["event"].endswith(k)), 0)
    return kind, SEVERITY_RANK.get(props["severity"], 0)


def find(per_hazard: int = 3) -> dict[str, list[Hotspot]]:
    feats, _ = nws.alerts_national(set(nws.EVENT_HAZARD))
    feats.sort(key=lambda x: _rank(x["properties"]), reverse=True)
    out: dict[str, list[Hotspot]] = {h: [] for h in nws.HAZARD_EVENTS}
    seen_offices: set[tuple[str, str]] = set()
    for feat in feats:
        p = feat["properties"]
        hazard = nws.EVENT_HAZARD[p["event"]]
        key = (hazard, p.get("senderName", ""))
        if len(out[hazard]) >= per_hazard or key in seen_offices:
            continue
        centroid = nws.geometry_centroid(feat.get("geometry"))
        if centroid is None and p.get("affectedZones"):
            centroid = nws.zone_centroid(p["affectedZones"][0])
        if centroid is None:
            continue
        seen_offices.add(key)
        out[hazard].append(Hotspot(hazard, p["event"], p["severity"], p["areaDesc"][:80],
                                   p.get("senderName", ""), round(centroid[0], 4), round(centroid[1], 4)))
    return out
