"""River gauges.

NOAA NWPS (api.water.noaa.gov/nwps/v1) is the primary source: it carries the NWS-defined
action/minor/moderate/major flood stages for each gauge, the current flood category, the
river forecast (crest), and upstream/downstream gauge links.
USGS water services gives the raw instantaneous reading for a cross-check.
Both are public with no key.
"""
from dataclasses import dataclass

from .http import Fetched, get_json

NWPS = "https://api.water.noaa.gov/nwps/v1"
USGS_IV = "https://waterservices.usgs.gov/nwis/iv/"

# NWPS category strings in ascending order of severity.
FLOOD_CATEGORIES = ["no_flooding", "action", "minor", "moderate", "major"]


def category_rank(cat: str | None) -> int:
    """-1 for unknown categories like out_of_service, not_defined or fcst_not_current."""
    return FLOOD_CATEGORIES.index(cat) if cat in FLOOD_CATEGORIES else -1


@dataclass
class GaugeSummary:
    lid: str  # NWS location id, e.g. DEWI4
    name: str
    lat: float
    lon: float
    observed_stage_ft: float | None
    observed_category: str
    observed_time: str | None
    forecast_stage_ft: float | None
    forecast_category: str


def gauges_near(lat: float, lon: float, box_deg: float = 0.4) -> tuple[list[GaugeSummary], Fetched]:
    f = get_json("nwps", f"{NWPS}/gauges", params={
        "bbox.xmin": lon - box_deg, "bbox.ymin": lat - box_deg,
        "bbox.xmax": lon + box_deg, "bbox.ymax": lat + box_deg, "srid": "EPSG_4326",
    })
    out = []
    for g in f.body.get("gauges", []):
        obs = (g.get("status") or {}).get("observed") or {}
        fc = (g.get("status") or {}).get("forecast") or {}
        out.append(GaugeSummary(
            lid=g["lid"], name=g["name"], lat=g["latitude"], lon=g["longitude"],
            observed_stage_ft=obs.get("primary") if obs.get("primaryUnit") == "ft" else None,
            observed_category=obs.get("floodCategory", "unknown"), observed_time=obs.get("validTime"),
            forecast_stage_ft=fc.get("primary") if fc.get("primaryUnit") == "ft" else None,
            forecast_category=fc.get("floodCategory", "unknown"),
        ))
    return out, f


@dataclass
class GaugeDetail:
    lid: str
    name: str
    usgs_id: str | None
    flood_stages_ft: dict[str, float]  # action/minor/moderate/major -> stage, as set by NWS
    upstream_lid: str | None
    downstream_lid: str | None


def gauge_detail(lid: str) -> tuple[GaugeDetail, Fetched]:
    f = get_json("nwps", f"{NWPS}/gauges/{lid}")
    b = f.body
    cats = ((b.get("flood") or {}).get("categories")) or {}
    stages = {k: v["stage"] for k, v in cats.items() if v.get("stage") not in (None, -9999)}
    return GaugeDetail(
        lid=b["lid"], name=b["name"], usgs_id=b.get("usgsId") or None, flood_stages_ft=stages,
        upstream_lid=b.get("upstreamLid") or None, downstream_lid=b.get("downstreamLid") or None,
    ), f


@dataclass
class StagePoint:
    time: str
    stage_ft: float


@dataclass
class StageFlow:
    observed: list[StagePoint]
    forecast: list[StagePoint]
    forecast_issued: str | None

    @property
    def latest(self) -> StagePoint | None:
        return self.observed[-1] if self.observed else None

    @property
    def crest(self) -> StagePoint | None:
        """Highest forecast stage. If it equals the first forecast value the river is falling."""
        return max(self.forecast, key=lambda p: p.stage_ft) if self.forecast else None


def stageflow(lid: str) -> tuple[StageFlow, Fetched]:
    f = get_json("nwps", f"{NWPS}/gauges/{lid}/stageflow")

    def pts(block):
        return [StagePoint(x["validTime"], x["primary"]) for x in (block or {}).get("data", [])
                if x.get("primary") not in (None, -999, -9999)]
    obs, fc = f.body.get("observed"), f.body.get("forecast")
    return StageFlow(pts(obs), pts(fc), (fc or {}).get("issuedTime")), f


@dataclass
class UsgsReading:
    site_id: str
    site_name: str
    stage_ft: float
    time: str
    provisional: bool


def usgs_latest_stage(site_id: str) -> tuple[UsgsReading | None, Fetched]:
    """Latest gage height (parameter 00065) from USGS instantaneous values."""
    f = get_json("usgs", USGS_IV, params={
        "format": "json", "sites": site_id, "parameterCd": "00065", "siteStatus": "active",
    })
    for ts in f.body["value"]["timeSeries"]:
        values = ts["values"][0]["value"]
        if values:
            v = values[-1]
            return UsgsReading(
                site_id=site_id, site_name=ts["sourceInfo"]["siteName"], stage_ft=float(v["value"]),
                time=v["dateTime"], provisional="P" in v.get("qualifiers", []),
            ), f
    return None, f
