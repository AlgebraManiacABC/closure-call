"""Runs the full pipeline (collect live data, then the rules engine) and prints the recommendation.

Run from the repo root:
    python scripts/evaluate.py                                   # current pin, next operating day
    python scripts/evaluate.py --lat 46.14 --lon -110.16 --day 2026-10-12
    python scripts/evaluate.py --lat 41.84 --lon -90.70 --day tomorrow --json out.json
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

from app.collect import collect  # noqa: E402
from app.facility import load_facility, repin_to_point  # noqa: E402
from app.rules import evaluate  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--lat", type=float, help="pin the facility here (with --lon)")
ap.add_argument("--lon", type=float)
ap.add_argument("--day", help="YYYY-MM-DD, 'today' or 'tomorrow'; default is the next operating day")
ap.add_argument("--json", help="write inputs and recommendation to this file")
args = ap.parse_args()

facility = load_facility()
if args.lat is not None and args.lon is not None:
    facility = repin_to_point(facility, args.lat, args.lon)
today = datetime.now(facility.tz).date()
day = {"today": today, "tomorrow": today + timedelta(days=1)}.get(args.day) if args.day in ("today", "tomorrow") \
    else date.fromisoformat(args.day) if args.day else facility.next_operating_day()

inputs, fetched = collect(facility, day)
rec = evaluate(inputs)

print(f"{rec.facility_label}, decision for {rec.day:%a %Y-%m-%d}, evaluated {rec.evaluated_at:%a %H:%M %Z}"
      f" ({len(fetched)} source fetches, rules {rec.rules_version})")
for n in inputs.notes:
    print(f"  note: {n}")
print(f"\nPOSTURE: {rec.posture_label}")
print(f"  {rec.timing_explanation}")
if rec.announce_by:
    print(f"  announce by {rec.announce_by:%a %H:%M}")
print(f"  confidence {rec.confidence}: " + " ".join(rec.confidence_reasons))

print("\nDecisive findings:")
for f in rec.decisive or []:
    span = f"{f.first_hour:%a %H:%M}-{f.last_hour + timedelta(hours=1):%H:%M}"
    if f.kind == "field":
        print(f"  {f.rule_id:22} {f.value} {f.unit} vs {f.threshold} (margin {f.margin:+}) {span}  [{inputs.sources[f.source_key].label}]")
    else:
        print(f"  {f.rule_id:22} {f.alert_event} ({f.alert_certainty}) {span}  [{inputs.sources['alerts'].label}]")
if not rec.decisive:
    print("  none")

print("\nThreshold checks (peak during commuting/working hours):")
for c in rec.checks:
    if c.peak is not None:
        print(f"  {c.rule_id:22} peak {c.peak:>7} {c.unit:10} vs {c.threshold:>6}  "
              f"{'CROSSED by' if c.crossed else 'short by  '} {abs(c.margin)}")
for a in rec.alerts_considered:
    print(f"  alert: {a.event} ({a.severity}/{a.certainty}) until {a.ends or 'further notice'}")
for a in rec.watches:
    print(f"  watch: {a.event} until {a.ends or 'further notice'}")
if inputs.gauge:
    g = inputs.gauge
    print(f"  gauge {g.lid} {g.name} ({g.distance_mi} mi): now {g.latest_stage_ft} ft, crest {g.crest_stage_ft} ft "
          f"at {g.crest_time}; stages {g.stages_ft}; USGS {g.usgs_stage_ft} ft")
if rec.flood_impact:
    print(f"  NWS impact at {rec.flood_impact.stage_ft} ft: {rec.flood_impact.statement}")

print("\nHourly level (0 none, 1 elevated, 2 hazardous):")
print("  " + " ".join(f"{h['start']:%H}:{h['level']}" for h in rec.hourly_levels))

print("\nWhat would change it:")
for c in rec.what_would_change:
    print(f"  {c.what:40} -> {c.detail}")
if not rec.what_would_change:
    print("  nothing within the tested ranges")

print(f"\nSummary: {rec.summary}")

if args.json:
    Path(args.json).write_text(
        '{"inputs": ' + inputs.model_dump_json() + ', "recommendation": ' + rec.model_dump_json() + "}",
        encoding="utf-8")
    print(f"\nWrote {args.json}")
