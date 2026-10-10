"""Full pipeline: collect live data, run the rules engine, then the Strands agent; optionally ask a question.

Run from the repo root:
    python scripts/brief.py --preset winter_montana --day 2026-10-12
    python scripts/brief.py --preset flood_iowa --day tomorrow --ask "Should people who live near the river come in?"
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import answer_question, investigate  # noqa: E402
from app.collect import collect  # noqa: E402
from app.facility import load_facility  # noqa: E402
from app.rules import evaluate  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--preset")
ap.add_argument("--day", help="YYYY-MM-DD, 'today' or 'tomorrow'; default is the next operating day")
ap.add_argument("--ask", action="append", default=[], help="a manager question; repeatable")
ap.add_argument("--force", action="store_true", help="run the agent even when the posture is normal")
args = ap.parse_args()

facility = load_facility()
if args.preset:
    facility = facility.repinned_to_preset(args.preset)
today = datetime.now(facility.tz).date()
day = {"today": today, "tomorrow": today + timedelta(days=1)}.get(args.day) if args.day in ("today", "tomorrow") \
    else date.fromisoformat(args.day) if args.day else facility.next_operating_day()

inputs, fetched = collect(facility, day)
rec = evaluate(inputs)
print(f"{rec.facility_label} ({inputs.elevation_ft} ft), {rec.day:%a %b %d}: {rec.posture_label} "
      f"[confidence {rec.confidence}]")

report, extra = investigate(inputs, rec, force=args.force)
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
    print(f"\nQ: {q}\nA: {answer_question(inputs, rec, report, q)}")
