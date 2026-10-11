"""National Weather Service API (api.weather.gov): point metadata, active alerts, gridpoint
forecast series and latest observations. Public, no key."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .http import Fetched, get_json

BASE = "https://api.weather.gov"

# Alert events for the three hazards we handle. Anything else is ignored by the rules engine.
HAZARD_EVENTS = {
    "flood": {"Flash Flood Warning", "Flood Warning", "Flood Watch", "Flood Advisory",
              "Flash Flood Watch", "Hydrologic Outlook"},
    "wind": {"High Wind Warning", "High Wind Watch", "Wind Advisory"},
    # Hurricanes and tropical storms are their own category: they bring wind, surge and flooding together.
    "tropical": {"Hurricane Warning", "Hurricane Watch", "Tropical Storm Warning", "Tropical Storm Watch",
                 "Storm Surge Warning", "Storm Surge Watch", "Extreme Wind Warning"},
    "winter": {"Blizzard Warning", "Ice Storm Warning", "Winter Storm Warning", "Winter Storm Watch",
               "Winter Weather Advisory", "Wind Chill Warning", "Wind Chill Advisory",
               "Extreme Cold Warning", "Cold Weather Advisory"},
}
EVENT_HAZARD = {event: hazard for hazard, events in HAZARD_EVENTS.items() for event in events}


# ---------- point ----------

@dataclass
class PointInfo:
    lat: float
    lon: float
    grid_id: str
    grid_x: int
    grid_y: int
    forecast_zone: str  # e.g. IAZ068
    county_zone: str  # e.g. IAC163
    time_zone: str
    city: str
    state: str
    stations_url: str


def point(lat: float, lon: float) -> tuple[PointInfo, Fetched]:
    f = get_json("nws", f"{BASE}/points/{lat:.4f},{lon:.4f}")
    p = f.body["properties"]
    loc = (p.get("relativeLocation") or {}).get("properties", {})
    info = PointInfo(
        lat=lat, lon=lon,
        grid_id=p["gridId"], grid_x=p["gridX"], grid_y=p["gridY"],
        forecast_zone=p["forecastZone"].rsplit("/", 1)[-1],
        county_zone=p["county"].rsplit("/", 1)[-1],
        time_zone=p["timeZone"],
        city=loc.get("city", ""), state=loc.get("state", ""),
        stations_url=p["observationStations"],
    )
    return info, f


# ---------- alerts ----------

@dataclass
class Alert:
    id: str
    event: str
    hazard: str | None  # flood / wind / winter, or None if not one we handle
    severity: str  # Extreme, Severe, Moderate, Minor, Unknown
    certainty: str  # Observed, Likely, Possible, Unlikely, Unknown
    urgency: str  # Immediate, Expected, Future, Past, Unknown
    message_type: str  # Alert, Update, Cancel
    sent: str
    effective: str
    onset: str | None
    expires: str
    ends: str | None
    headline: str
    nws_headline: str
    area_desc: str
    sender_name: str
    vtec: str
    description: str  # free text; the agent extracts magnitudes from here, quoting the sentence
    instruction: str
    max_wind_gust: str | None  # only present on some convective alerts
    affected_zones: list[str]
    has_geometry: bool


def _first(params: dict, key: str) -> str | None:
    v = params.get(key)
    return v[0] if v else None


def parse_alert(feature: dict) -> Alert:
    p = feature["properties"]
    params = p.get("parameters") or {}
    return Alert(
        id=p["id"], event=p["event"], hazard=EVENT_HAZARD.get(p["event"]),
        severity=p["severity"], certainty=p["certainty"], urgency=p["urgency"],
        message_type=p["messageType"], sent=p["sent"], effective=p["effective"],
        onset=p.get("onset"), expires=p["expires"], ends=p.get("ends"),
        headline=p.get("headline") or "", nws_headline=_first(params, "NWSheadline") or "",
        area_desc=p.get("areaDesc") or "", sender_name=p.get("senderName") or "",
        vtec=_first(params, "VTEC") or "",
        description=p.get("description") or "", instruction=p.get("instruction") or "",
        max_wind_gust=_first(params, "maxWindGust"),
        affected_zones=[z.rsplit("/", 1)[-1] for z in p.get("affectedZones", [])],
        has_geometry=feature.get("geometry") is not None,
    )


def alerts_for_point(lat: float, lon: float) -> tuple[list[Alert], Fetched]:
    """Active, actual alerts covering this point (NWS resolves zones and polygons for us)."""
    f = get_json("nws", f"{BASE}/alerts/active",
                 params={"point": f"{lat:.4f},{lon:.4f}", "status": "actual"})
    return [parse_alert(x) for x in f.body["features"]], f


def alerts_national(events: set[str] | None = None) -> tuple[list[dict], Fetched]:
    """All active actual alerts nationwide, as raw features (geometry kept), optionally filtered by event."""
    f = get_json("nws", f"{BASE}/alerts/active",
                 params={"status": "actual", "message_type": "alert,update"})
    feats = f.body["features"]
    if events:
        feats = [x for x in feats if x["properties"]["event"] in events]
    return feats, f


def zone_centroid(zone_url: str) -> tuple[float, float] | None:
    """Rough centroid (lat, lon) of a forecast zone, used to re-pin the demo facility."""
    f = get_json("nws", zone_url)
    return geometry_centroid(f.body.get("geometry"))


def geometry_centroid(geom: dict | None) -> tuple[float, float] | None:
    if not geom:
        return None
    coords = geom["coordinates"]
    if geom["type"] == "Polygon":
        ring = coords[0]
    elif geom["type"] == "MultiPolygon":
        ring = max((poly[0] for poly in coords), key=len)
    else:
        return None
    lon = sum(c[0] for c in ring) / len(ring)
    lat = sum(c[1] for c in ring) / len(ring)
    return lat, lon


# ---------- gridpoint forecast ----------

_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")

KMH_TO_MPH = 0.621371
MM_TO_IN = 1 / 25.4


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


# field -> (converter, unit after conversion)
GRID_FIELDS = {
    "windSpeed": (lambda v: v * KMH_TO_MPH, "mph"),
    "windGust": (lambda v: v * KMH_TO_MPH, "mph"),
    "snowfallAmount": (lambda v: v * MM_TO_IN, "in"),
    "iceAccumulation": (lambda v: v * MM_TO_IN, "in"),
    "quantitativePrecipitation": (lambda v: v * MM_TO_IN, "in"),
    "temperature": (c_to_f, "F"),
    "windChill": (c_to_f, "F"),
    "apparentTemperature": (c_to_f, "F"),
    "visibility": (lambda v: v / 1609.34, "mi"),
    "probabilityOfPrecipitation": (lambda v: v, "%"),
    "potentialOf40mphWindGusts": (lambda v: v, "%"),
    "potentialOf50mphWindGusts": (lambda v: v, "%"),
    "potentialOf60mphWindGusts": (lambda v: v, "%"),
}


@dataclass
class SeriesPoint:
    start: datetime
    end: datetime
    value: float


def _parse_valid_time(s: str) -> tuple[datetime, datetime]:
    start_s, dur_s = s.split("/")
    start = datetime.fromisoformat(start_s)
    m = _DURATION.match(dur_s)
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return start, start + timedelta(days=d, hours=h, minutes=mi)


def gridpoint(info: PointInfo) -> Fetched:
    return get_json("nws", f"{BASE}/gridpoints/{info.grid_id}/{info.grid_x},{info.grid_y}")


def grid_series(grid: Fetched, field: str) -> tuple[list[SeriesPoint], str]:
    """One gridpoint field as a series in US units (mph, in, F, mi, %)."""
    convert, unit = GRID_FIELDS[field]
    raw = grid.body["properties"].get(field) or {"values": []}
    out = []
    for v in raw["values"]:
        if v["value"] is None:
            continue
        start, end = _parse_valid_time(v["validTime"])
        out.append(SeriesPoint(start, end, convert(v["value"])))
    return out, unit


def window(series: list[SeriesPoint], start: datetime, end: datetime) -> list[SeriesPoint]:
    return [p for p in series if p.end > start and p.start < end]


def series_max(series: list[SeriesPoint], start: datetime, end: datetime) -> SeriesPoint | None:
    w = window(series, start, end)
    return max(w, key=lambda p: p.value) if w else None


def series_min(series: list[SeriesPoint], start: datetime, end: datetime) -> SeriesPoint | None:
    w = window(series, start, end)
    return min(w, key=lambda p: p.value) if w else None


def series_total(series: list[SeriesPoint], start: datetime, end: datetime) -> float:
    """Sum of an accumulation field (snow, ice, rain), prorating periods that straddle the window."""
    total = 0.0
    for p in window(series, start, end):
        span = (p.end - p.start).total_seconds()
        overlap = (min(p.end, end) - max(p.start, start)).total_seconds()
        total += p.value * overlap / span if span else 0
    return total


# ---------- observations ----------

@dataclass
class Observation:
    station_id: str
    station_name: str
    timestamp: str
    temperature_f: float | None
    wind_speed_mph: float | None
    wind_gust_mph: float | None
    visibility_mi: float | None
    text: str


def _val(props: dict, key: str, convert=lambda v: v):
    v = (props.get(key) or {}).get("value")
    return None if v is None else convert(v)


def latest_observation(info: PointInfo) -> tuple[Observation | None, list[Fetched]]:
    """Latest observation from the nearest station that has reported recently."""
    stations = get_json("nws", info.stations_url)
    fetched = [stations]
    for feat in stations.body["features"][:3]:
        sid = feat["properties"]["stationIdentifier"]
        f = get_json("nws", f"{BASE}/stations/{sid}/observations/latest")
        fetched.append(f)
        p = f.body["properties"]
        ts = datetime.fromisoformat(p["timestamp"])
        if datetime.now(timezone.utc) - ts > timedelta(hours=3):
            continue
        return Observation(
            station_id=sid, station_name=feat["properties"]["name"], timestamp=p["timestamp"],
            temperature_f=_val(p, "temperature", c_to_f),
            wind_speed_mph=_val(p, "windSpeed", lambda v: v * KMH_TO_MPH),
            wind_gust_mph=_val(p, "windGust", lambda v: v * KMH_TO_MPH),
            visibility_mi=_val(p, "visibility", lambda v: v / 1609.34),
            text=p.get("textDescription") or "",
        ), fetched
    return None, fetched
