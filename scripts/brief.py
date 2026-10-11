"""Full pipeline: collect live data, run the rules engine, then the Strands agent; optionally ask a question.

Run from the repo root:
    python scripts/brief.py --lat 46.14 --lon -110.16 --day 2026-10-12
    python scripts/brief.py --lat 41.84 --lon -90.70 --day tomorrow --ask "Should people who live near the river come in?"
    python scripts/brief.py --lat 46.14 --lon -110.16 --day 2026-10-12 --record --decide approve --by "Facility manager"
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import answer_question, investigate  # noqa: E402
from app.collect import collect  # noqa: E402
from app.facility import load_facility, repin_to_point  # noqa: E402
from app.record import get_store  # noqa: E402
from app.rules import evaluate  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--lat", type=float, help="pin the facility here (with --lon)")
ap.add_argument("--lon", type=float)
ap.add_argument("--day", help="YYYY-MM-DD, 'today' or 'tomorrow'; default is the next operating day")
ap.add_argument("--ask", action="append", default=[], help="a manager question; repeatable")
ap.add_argument("--force", action="store_true", help="run the agent even when the posture is normal")
ap.add_argument("--record", action="store_true", help="write everything to the decision record")
ap.add_argument("--decide", choices=["approve", "override"], help="record the manager's decision (needs --record)")
ap.add_argument("--by", default="Facility manager", help="who decided or asked")
ap.add_argument("--posture", help="override posture")
ap.add_argument("--delay-hours", type=int)
ap.add_argument("--dismissal-time")
ap.add_argument("--reason", default="")
ap.add_argument("--db", help="'local' for the SQLite file instead of DATABASE_URL")
args = ap.parse_args()

facility = load_facility()
if args.lat is not None and args.lon is not None:
    facility = repin_to_point(facility, args.lat, args.lon)
today = datetime.now(facility.tz).date()
day = {"today": today, "tomorrow": today + timedelta(days=1)}.get(args.day) if args.day in ("today", "tomorrow") \
    else date.fromisoformat(args.day) if args.day else facility.next_operating_day()

inputs, fetched = collect(facility, day)
rec = evaluate(inputs)
print(f"{rec.facility_label} ({inputs.elevation_ft} ft), {rec.day:%a %b %d}: {rec.posture_label} "
      f"[confidence {rec.confidence}]")

store = get_store(args.db) if args.record else None
if store:
    ref = store.record_evaluation(inputs, rec, fetched)
    print(f"Recorded evaluation {ref.id} (ledger seq {ref.seq}) in {store.url_safe}")

report, extra = investigate(inputs, rec, force=args.force)
if store:
    store.record_agent_report(ref.id, report, extra)
print(f"\nAgent: {report.reason} ({report.duration_s}s, {report.tokens.get('totalTokens', 0)} tokens)")
print("Tool calls: " + "; ".join(report.tool_calls))
print("\nEvidence (quotes verified word for word against the alert text):")
for e in report.evidence:
    print(f"  [{e.alert_ref} {e.alert_event}] {e.kind} = {e.value} {e.unit}: \"{e.quote}\"" +
          (f"\n      note: {e.note}" if e.note else ""))
for q in report.rejected_quotes:
    print(f"  REJECTED (not in alert text): \"{q}\"")
for l in report.lookups:
    print(f"\nLookup {l.name} ({l.source}):\n  " + l.result.replace("\n", "\n  "))
for w in report.what_ifs:
    print(f"\nWhat if {w.change}: {w.label}")
print(f"\nRATIONALE:\n{report.rationale}")
for c in report.caveats:
    print(f"  - {c}")

for q in args.ask:
    a = answer_question(inputs, rec, report, q)
    print(f"\nQ: {q}\nA: {a}")
    if store:
        store.record_question(ref.id, q, a, args.by)

if store and args.decide:
    d = store.record_decision(ref.id, args.decide, args.by, args.reason, args.posture, args.delay_hours,
                              args.dismissal_time)
    print(f"\nRecorded decision ({args.decide}) by {args.by}, ledger seq {d.seq}")
if store:
    print(f"Evaluation id: {ref.id}  (python scripts/history.py --show {ref.id})")
