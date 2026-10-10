"""Pulls every ground-truth source for one location and prints what came back.

Run from the repo root:
    python scripts/check_sources.py                      # default: De Witt, IA
    python scripts/check_sources.py --lat 35.6 --lon -82.5
    python scripts/check_sources.py --hotspots           # where is each hazard active right now?
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

from app.sources import hotspots, nws, water  # noqa: E402


def show_hotspots():
    for hazard, spots in hotspots.find().items():
        print(f"\n== {hazard} ==")
        for s in spots:
            print(f"  {s.event:26} {s.severity:8} {s.lat:9.4f},{s.lon:10.4f}  {s.sender_name}: {s.area_desc}")


def show_location(lat: float, lon: float):
    info, f = nws.point(lat, lon)
    print(f"== Point {lat},{lon}: {info.city}, {info.state}  grid {info.grid_id}/{info.grid_x},{info.grid_y}"
          f"  zone {info.forecast_zone}  county {info.county_zone}  tz {info.time_zone}")
    print(f"   fetched {f.fetched_at:%Y-%m-%d %H:%M:%SZ} from {f.url}")

    alerts, f = nws.alerts_for_point(lat, lon)
    print(f"\n== Active alerts at point: {len(alerts)}")
    for a in alerts:
        print(f"  [{a.hazard or '-'}] {a.event}: {a.severity}/{a.certainty}/{a.urgency}  "
              f"onset {a.onset}  ends {a.ends or a.expires}")
        print(f"      {a.nws_headline or a.headline}")

    grid = nws.gridpoint(info)
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=24)
    print(f"\n== Gridpoint forecast, next 24 h (updated {grid.body['properties'].get('updateTime')})")
    for field in ("windGust", "windSpeed", "potentialOf50mphWindGusts"):
        s, unit = nws.grid_series(grid, field)
        m = nws.series_max(s, now, end)
        print(f"  max {field:28} {m.value:6.1f} {unit} at {m.start:%a %H:%MZ}" if m else f"  {field}: no data")
    for field in ("snowfallAmount", "iceAccumulation", "quantitativePrecipitation"):
        s, unit = nws.grid_series(grid, field)
        print(f"  total {field:26} {nws.series_total(s, now, end):6.2f} {unit}")
    for field in ("windChill", "temperature"):
        s, unit = nws.grid_series(grid, field)
        m = nws.series_min(s, now, end)
        print(f"  min {field:28} {m.value:6.1f} {unit} at {m.start:%a %H:%MZ}" if m else f"  {field}: no data")

    obs, _ = nws.latest_observation(info)
    print("\n== Latest observation")
    if obs:
        print(f"  {obs.station_id} {obs.station_name} at {obs.timestamp}: {obs.text}, "
              f"temp {obs.temperature_f and round(obs.temperature_f)} F, "
              f"wind {obs.wind_speed_mph and round(obs.wind_speed_mph)} mph, "
              f"gust {obs.wind_gust_mph and round(obs.wind_gust_mph)} mph")
    else:
        print("  no recent observation from the nearest stations")

    gauges, _ = water.gauges_near(lat, lon)
    print(f"\n== NWPS river gauges within ~0.4 deg: {len(gauges)}")
    for g in sorted(gauges, key=lambda g: water.category_rank(g.observed_category), reverse=True):
        print(f"  {g.lid:6} {g.name[:45]:45} {g.observed_stage_ft} ft  obs={g.observed_category}  "
              f"fcst={g.forecast_category}")

    worst = max(gauges, key=lambda g: water.category_rank(g.observed_category), default=None)
    if worst and water.category_rank(worst.observed_category) >= 1:
        detail, _ = water.gauge_detail(worst.lid)
        sf, _ = water.stageflow(worst.lid)
        print(f"\n== Worst gauge {worst.lid}: {detail.name}")
        print(f"  NWS flood stages (ft): {detail.flood_stages_ft}")
        print(f"  latest {sf.latest.stage_ft} ft at {sf.latest.time}" if sf.latest else "  no observed data")
        print(f"  forecast crest {sf.crest.stage_ft} ft at {sf.crest.time} (issued {sf.forecast_issued})"
              if sf.crest else "  no forecast")
        print(f"  upstream {detail.upstream_lid}, downstream {detail.downstream_lid}")
        if detail.usgs_id:
            r, _ = water.usgs_latest_stage(detail.usgs_id)
            if r:
                print(f"  USGS {r.site_id} cross-check: {r.stage_ft} ft at {r.time}"
                      f"{' (provisional)' if r.provisional else ''}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=41.8236)
    ap.add_argument("--lon", type=float, default=-90.5385)
    ap.add_argument("--hotspots", action="store_true")
    args = ap.parse_args()
    show_hotspots() if args.hotspots else show_location(args.lat, args.lon)
