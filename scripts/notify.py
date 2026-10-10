"""Builds the notification package for a recorded decision, and optionally approves sending.

Run from the repo root:
    python scripts/notify.py --evaluation <id>                 # build and record the draft package
    python scripts/notify.py --evaluation <id> --send --by "Reese Okafor (facility manager)"
    add --db local to use the SQLite file, --no-agent to use code templates only
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.facility import Pin, load_facility, load_roster  # noqa: E402
from app.notify import Decided, build_package  # noqa: E402
from app.record import get_store  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--evaluation", help="evaluation id; default is the latest one with a decision")
ap.add_argument("--send", action="store_true", help="approve sending the latest package (simulated)")
ap.add_argument("--by", default="Facility manager")
ap.add_argument("--no-agent", action="store_true")
ap.add_argument("--db", help="'local' for the SQLite file instead of DATABASE_URL")
args = ap.parse_args()

store = get_store(args.db)
eid = args.evaluation or next((h["evaluation_id"] for h in store.history() if h["decision"]), None)
if not eid:
    sys.exit("No evaluation with a decision in the record.")
rec_data = store.load(eid)
inputs, rec = rec_data["inputs"], rec_data["recommendation"]

if args.send:
    ref, msgs = store.record_send(eid, args.by)
    print(f"Send approved by {args.by} (ledger seq {ref.seq}). {len(msgs)} messages placed in the outbox "
          "(simulated; nothing left the system):")
    for m in msgs[:6]:
        print(f"  {m.channel:5} -> {m.name:22} {m.to:28} {m.body[:90]}")
    print(f"  ... {len(msgs) - 6} more" if len(msgs) > 6 else "")
    sys.exit()

dp = store.latest_decision(eid)["payload"]
decided = Decided(posture=dp["posture"], label=dp["posture_label"], delay_hours=dp["delay_hours"],
                  dismissal_time=dp["dismissal_time"], action=dp["action"])
facility = load_facility()
facility = facility.repinned(Pin(inputs.lat, inputs.lon, inputs.facility_label.split(", ", 1)[1], inputs.time_zone))
pkg = build_package(facility, load_roster(), inputs, rec, decided, use_agent=not args.no_agent)
ref = store.record_notification_package(eid, pkg)

print(f"{pkg.facility_label}, {pkg.day:%a %b %d}: {decided.label} ({decided.action}); cause: {pkg.reason_phrase}")
print(f"Send by {pkg.send_by:%a %H:%M} | {pkg.counts} | drafting: {pkg.drafting} | recorded seq {ref.seq}\n")
print("NOTIFY:")
for r in pkg.recipients:
    if r.notify:
        print(f"  {r.employee_id} {r.name:22} {r.group:22} {'+'.join(r.channels):9} {' '.join(r.reasons)}")
        for n in r.personal_notes:
            print(f"       note: {n}")
print("\nSKIP:")
for r in pkg.recipients:
    if not r.notify:
        print(f"  {r.employee_id} {r.name:22} {' '.join(r.reasons)}")
print("\nGROUP MESSAGES:")
for g in pkg.groups:
    print(f"\n[{g.group}] {g.description}: {g.count} people, {g.source}"
          + (f" (agent draft replaced: {'; '.join(g.problems)})" if g.problems else ""))
    print(f"  SMS ({len(g.sms)} chars): {g.sms}")
    print(f"  Email subject: {g.email_subject}")
    print("  " + g.email_body.replace("\n", "\n  "))
print(f"\nTo approve sending: python scripts/notify.py --evaluation {eid} --send --by \"NAME\""
      + (f" --db {args.db}" if args.db else ""))
