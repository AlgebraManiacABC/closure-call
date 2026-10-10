"""Prints the facility and where each person on its roster is on the decision day.

Run from the repo root:
    python scripts/show_facility.py                    # next operating day at the current pin
    python scripts/show_facility.py --day 2026-10-13 --preset winter_montana
"""
import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.facility import load_facility, load_roster, roster_for_day  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--day", type=date.fromisoformat)
ap.add_argument("--preset")
args = ap.parse_args()

facility = load_facility()
if args.preset:
    facility = facility.repinned_to_preset(args.preset)
day = args.day or facility.next_operating_day()

print(f"{facility.display_name}  ({facility.pin.lat}, {facility.pin.lon}, {facility.pin.time_zone})")
print(f"Hours {facility.open:%H:%M}-{facility.close:%H:%M}, decision day {day:%a %Y-%m-%d}"
      f"{'' if facility.is_operating_day(day) else '  (NOT an operating day)'}")

statuses = roster_for_day(load_roster(), facility, day)
print(f"\n{len(statuses)} people tied to this facility: {dict(Counter(s.location.split(':')[0] for s in statuses))}")
for s in statuses:
    e = s.employee
    print(f"  {e.id} {e.name:22} {e.agency:4} {s.location:42} {', '.join(s.flags)}")
    if s.location != "office" or s.detailed_in:
        print(f"       {s.reason}")
