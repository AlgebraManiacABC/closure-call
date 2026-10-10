"""Fetches every source for a facility and decision day and builds the rules engine's Inputs.

All network access lives here; the engine itself stays pure. Returns the raw Fetched responses
too, so the decision record can store exactly what was retrieved.
"""
import math
from datetime import date, datetime, time, timedelta, timezone

from .facility import Facility
from .rules import AlertIn, GaugeIn, HourIn, Impact, Inputs, SourceRef
from .sources import nws, water
from .sources.http import Fetched, SourceError

HOUR = timedelta(hours=1)
GAUGE_MAX_MILES = 25


def _ref(f: Fetched, label: str, issued: str | None = None) -> SourceRef:
    return SourceRef(source=f.source, label=label, url=f.url, fetched_at=f.fetched_at, issued=issued)


def _miles(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def _hourly(series: list[nws.SeriesPoint], accumulate: bool) -> dict[datetime, float]:
    """Spread gridpoint periods onto UTC hour starts: rates repeat, accumulations are divided."""
    out = {}
    for p in series:
        n = max(1, round((p.end - p.start) / HOUR))
        for k in range(n):
            out[(p.start + k * HOUR).astimezone(timezone.utc)] = p.value / n if accumulate else p.value
    return out


def _interp(points: list[tuple[datetime, float]], t: datetime) -> tuple[float | None, bool]:
    """Linear interpolation of river stage; past the last point, hold its value and flag it."""
    if not points:
        return None, False
    if t <= points[0][0]:
        return points[0][1], False
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            frac = (t - t0) / (t1 - t0) if t1 > t0 else 0
            return round(v0 + (v1 - v0) * frac, 2), False
    return points[-1][1], True


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _gauge(facility: Facility, lat: float, lon: float, notes: list[str], fetched: list[Fetched],
           sources: dict) -> tuple[GaugeIn | None, list[tuple[datetime, float]]]:
    lid = facility.pin.flood_gauge_lid
    if not lid:
        near, f = water.gauges_near(lat, lon)
        fetched.append(f)
        candidates = sorted((g for g in near if water.category_rank(g.observed_category) >= 0),
                            key=lambda g: _miles(lat, lon, g.lat, g.lon))
        if not candidates:
            notes.append("No river gauge with NWS flood stages near the facility; flood relies on alerts only.")
            return None, []
        lid = candidates[0].lid
    detail, f = water.gauge_detail(lid)
    fetched.append(f)
    dist = _miles(lat, lon, detail.lat, detail.lon)
    if dist > GAUGE_MAX_MILES:
        notes.append(f"Nearest gauge {lid} is {dist:.0f} miles away; not used.")
        return None, []
    sf, f = water.stageflow(lid)
    fetched.append(f)
    sources["gauge"] = _ref(f, f"NOAA NWPS gauge {lid} ({detail.name})", sf.forecast_issued)
    points = sorted([(_parse(p.time), p.stage_ft) for p in sf.observed] +
                    [(_parse(p.time), p.stage_ft) for p in sf.forecast
                     if not sf.observed or _parse(p.time) > _parse(sf.observed[-1].time)])
    g = GaugeIn(
        lid=lid, name=detail.name, distance_mi=round(dist, 1), stages_ft=detail.flood_stages_ft,
        impacts=[Impact(stage_ft=s, statement=t) for s, t in detail.impacts],
        latest_stage_ft=sf.latest.stage_ft if sf.latest else None, latest_time=sf.latest.time if sf.latest else None,
        crest_stage_ft=sf.crest.stage_ft if sf.crest else None, crest_time=sf.crest.time if sf.crest else None,
        forecast_issued=sf.forecast_issued, usgs_site=detail.usgs_id)
    if detail.usgs_id:
        try:
            r, f = water.usgs_latest_stage(detail.usgs_id)
            fetched.append(f)
            if r:
                g.usgs_stage_ft, g.usgs_time = r.stage_ft, r.time
                sources["usgs"] = _ref(f, f"USGS gauge {r.site_id} ({r.site_name})")
        except SourceError as e:
            notes.append(f"USGS cross-check unavailable: {e}")
    return g, points


def collect(facility: Facility, day: date, now: datetime | None = None) -> tuple[Inputs, list[Fetched]]:
    tz = facility.tz
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    lat, lon = facility.pin.lat, facility.pin.lon
    fetched: list[Fetched] = []
    sources: dict[str, SourceRef] = {}
    notes: list[str] = []

    info, f = nws.point(lat, lon)
    fetched.append(f)
    sources["point"] = _ref(f, f"NWS point {lat:.4f},{lon:.4f}")

    alerts, f = nws.alerts_for_point(lat, lon)
    fetched.append(f)
    sources["alerts"] = _ref(f, f"NWS active alerts for {lat:.4f},{lon:.4f}")
    alert_ins = [AlertIn(id=a.id, event=a.event, hazard=a.hazard, severity=a.severity, certainty=a.certainty,
                         urgency=a.urgency, starts=_parse(a.onset or a.effective),
                         ends=_parse(a.ends) if a.ends else None, headline=a.nws_headline or a.headline,
                         sender=a.sender_name, sent=a.sent, area=a.area_desc, description=a.description,
                         instruction=a.instruction)
                 for a in alerts if a.hazard and a.message_type != "Cancel"]

    grid = nws.gridpoint(info)
    fetched.append(grid)
    sources["grid"] = _ref(grid, f"NWS gridpoint forecast {info.grid_id}/{info.grid_x},{info.grid_y}",
                           grid.body["properties"].get("updateTime"))
    series = {
        "gust_mph": _hourly(nws.grid_series(grid, "windGust")[0], False),
        "sustained_mph": _hourly(nws.grid_series(grid, "windSpeed")[0], False),
        "snow_in": _hourly(nws.grid_series(grid, "snowfallAmount")[0], True),
        "ice_in": _hourly(nws.grid_series(grid, "iceAccumulation")[0], True),
        "wind_chill_f": _hourly(nws.grid_series(grid, "windChill")[0], False),
        "temperature_f": _hourly(nws.grid_series(grid, "temperature")[0], False),
    }
    elev_m = (grid.body["properties"].get("elevation") or {}).get("value")

    gauge, stage_points = _gauge(facility, lat, lon, notes, fetched, sources)

    start = datetime.combine(day - timedelta(days=1), time(12), tzinfo=tz)
    end = datetime.combine(day, time(21), tzinfo=tz)
    hours = []
    t = start
    while t < end:
        key = t.astimezone(timezone.utc)
        row = {k: (round(v[key], 3) if key in v else None) for k, v in series.items()}
        stage, extrap = _interp(stage_points, key)
        hours.append(HourIn(start=t, gauge_stage_ft=stage, gauge_extrapolated=extrap, **row))
        t += HOUR

    obs = None
    try:
        o, fs = nws.latest_observation(info)
        fetched.extend(fs)
        if o:
            obs = o.__dict__
            sources["obs"] = _ref(fs[-1], f"NWS observation {o.station_id} ({o.station_name})", o.timestamp)
    except SourceError as e:
        notes.append(f"Latest observation unavailable: {e}")

    if not facility.is_operating_day(day):
        notes.append(f"{day:%a %b %d} is not a scheduled operating day; evaluated as if it were.")

    inputs = Inputs(
        facility_id=facility.id, facility_label=facility.display_name, lat=lat, lon=lon,
        elevation_ft=round(elev_m * 3.28084) if elev_m is not None else None,
        time_zone=facility.pin.time_zone, day=day, evaluated_at=now, open=facility.open, close=facility.close,
        morning_commute=facility.morning_commute, evening_commute=facility.evening_commute,
        decide_by=facility.delay_or_closure_by, hours=hours, alerts=alert_ins, gauge=gauge,
        observation=obs, sources=sources, notes=notes)
    return inputs, fetched
