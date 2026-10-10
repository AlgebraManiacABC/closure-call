"""Generates data/roster.json: a synthetic roster of about 40 people for the demo Service Center.

The first block is hand-written so every notify/skip rule has someone to show it off. The rest
is filler from a fixed seed, so re-running gives the same file. All people are fictional;
phone numbers use the 555-01xx range reserved for fiction and emails use the .example domain.

Run from the repo root:  python scripts/make_roster.py
"""
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACILITY = "usda-sc-001"
rng = random.Random(2026)

# Date ranges cover the whole hackathon week so the cases are live whichever day is evaluated.
WEEK = {"start": "2026-10-05", "end": "2026-10-23"}

FIRST = ["Avery", "Jordan", "Casey", "Morgan", "Riley", "Taylor", "Quinn", "Reese", "Rowan", "Emerson",
         "Hayden", "Parker", "Sawyer", "Logan", "Dakota", "Skyler", "Peyton", "Cameron", "Harper", "Blake",
         "Drew", "Elliot", "Finley", "Jamie", "Kendall", "Lane", "Marlow", "Noel", "Oakley", "Remy",
         "Sage", "Tatum", "Wren", "Arden", "Bellamy", "Corin", "Darby", "Ellis", "Frankie", "Greer"]
LAST = ["Halvorsen", "Okafor", "Brennan", "Lindqvist", "Mendoza", "Pruitt", "Castellano", "Yoder",
        "Nakamura", "Abernathy", "Kowalski", "Delacroix", "Whitfield", "Ostrander", "Ruiz", "Fenwick",
        "Haugen", "Iverson", "Jablonski", "Kessler", "Larkin", "Moravec", "Novak", "Ortega", "Pellerin",
        "Quist", "Rasmussen", "Sorensen", "Thibodeaux", "Underhill", "Vasquez", "Wojcik", "Ambrose",
        "Bergstrom", "Cardenas", "Dvorak", "Engstrom", "Fairbanks", "Gustafson", "Holloway"]

TITLES = {
    "FSA": ["Program Technician", "Program Technician", "Farm Loan Officer", "Farm Loan Program Technician",
            "Program Analyst"],
    "NRCS": ["Soil Conservationist", "Soil Conservation Technician", "Engineering Technician",
             "Resource Conservationist"],
    "RD": ["Loan Technician", "Area Specialist"],
}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]

_names = iter(zip(rng.sample(FIRST, len(FIRST)), rng.sample(LAST, len(LAST))))  # 40 unique names
_phone = iter(range(100, 200))


def person(eid, title, agency, preferred="sms", **kw):
    first, last = next(_names)
    emp = {
        "id": eid,
        "name": f"{first} {last}",
        "title": title,
        "agency": agency,
        "duty_station": FACILITY,
        "detail": None,
        "essential": False,
        "telework_eligible": True,
        "telework_days": [],
        "planned_leave": [],
        "accessibility_needs": [],
        "shift": {"start": "08:00", "end": "16:30"},
        "commute_miles": rng.randint(3, 35),
        "commute_flood_prone": False,
        "contact": {
            "phone": f"+1-555-01{next(_phone) - 100:02d}",
            "email": f"{first.lower()}.{last.lower()}@usda.example",
            "preferred": preferred,
        },
        "notes": "",
    }
    emp.update(kw)
    return emp


people = [
    # Decision maker. Essential: reports under every posture to secure the building.
    person("E001", "Facility Operations Specialist (facility manager)", "FSA", essential=True,
           telework_eligible=True, shift={"start": "07:00", "end": "15:30"},
           notes="Receives the recommendation and approves or overrides it."),
    person("E002", "County Executive Director", "FSA", essential=True, telework_days=["Fri"],
           notes="Senior FSA official on site; essential for continuity of program deadlines."),
    person("E003", "IT Specialist", "FSA", essential=True, telework_eligible=False,
           shift={"start": "07:00", "end": "15:30"},
           notes="Essential: keeps the network up so teleworkers can connect during a closure."),
    person("E004", "District Conservationist", "NRCS", telework_days=["Mon", "Thu"]),
    person("E005", "Area Director", "RD", telework_days=["Wed"]),

    # On detail elsewhere this week: assigned here but should not be told to come in.
    person("E006", "Farm Loan Manager", "FSA",
           detail={"location": "USDA FSA State Office, Des Moines", **WEEK},
           notes="On detail to the state office; skip unless the posture affects them there."),
    person("E007", "Soil Conservationist", "NRCS",
           detail={"location": "NRCS Area Office, Cedar Rapids", **WEEK}),

    # Detailed IN from another office: not on this building's own roster, but must be notified.
    person("E008", "Rural Development Loan Specialist", "RD",
           duty_station="USDA Service Center, Muscatine",
           detail={"location": FACILITY, **WEEK},
           notes="Assigned to Muscatine, on detail here this week. Easy to miss with the building roster alone."),
    person("E009", "Engineering Technician", "NRCS", telework_eligible=False,
           duty_station="NRCS Field Office, Maquoketa",
           detail={"location": FACILITY, **WEEK}),

    # Already teleworking every weekday: no commute; their message is about whether work continues.
    person("E010", "Program Analyst", "FSA", telework_days=WEEKDAYS,
           notes="Full-time telework agreement."),
    person("E011", "Area Specialist", "RD", telework_days=WEEKDAYS, commute_miles=48),

    # On planned leave this week: skip, with the reason recorded.
    person("E012", "Program Technician", "FSA", planned_leave=[WEEK]),

    # Accessibility needs that change what a posture means for them.
    person("E013", "Program Technician", "FSA", telework_eligible=False,
           accessibility_needs=[{"type": "mobility",
                                 "note": "Uses a wheelchair; snow or ice on the lot and ramp is a barrier."}]),
    person("E014", "Soil Conservation Technician", "NRCS",
           accessibility_needs=[{"type": "hearing", "note": "Deaf; text or email only, never voice calls."}]),
    person("E015", "Farm Loan Officer", "FSA",
           accessibility_needs=[{"type": "vision", "note": "Low vision; does not drive in darkness or heavy precipitation, relies on a ride."}]),
    person("E016", "Loan Technician", "RD",
           accessibility_needs=[{"type": "medical", "note": "Condition worsened by extreme cold; needs reduced outdoor exposure."}]),

    # Commute crosses a flood-prone route.
    person("E017", "Soil Conservation Technician", "NRCS", commute_miles=28, commute_flood_prone=True,
           telework_eligible=False, notes="Route crosses the river on a low bridge."),
    person("E018", "Program Technician", "FSA", commute_miles=22, commute_flood_prone=True,
           preferred="email"),

    # Early shift: a 2-hour delay means something different for them.
    person("E019", "Farm Loan Program Technician", "FSA", shift={"start": "06:30", "end": "15:00"},
           telework_eligible=False),
]

# Filler to about 40 people.
agency_mix = ["FSA"] * 9 + ["NRCS"] * 8 + ["RD"] * 4
for n, agency in enumerate(agency_mix, start=len(people) + 1):
    eligible = rng.random() < 0.75
    people.append(person(
        f"E{n:03d}", rng.choice(TITLES[agency]), agency,
        telework_eligible=eligible,
        telework_days=sorted(rng.sample(WEEKDAYS, rng.choice([0, 1, 1, 2])), key=WEEKDAYS.index) if eligible else [],
        commute_flood_prone=rng.random() < 0.1,
        shift=rng.choice([{"start": "08:00", "end": "16:30"}] * 3 + [{"start": "07:30", "end": "16:00"}]),
        preferred=rng.choice(["sms", "sms", "email"]),
    ))

out = ROOT / "data" / "roster.json"
out.write_text(json.dumps({"facility_id": FACILITY, "employees": people}, indent=2) + "\n", encoding="utf-8")
print(f"Wrote {len(people)} employees to {out.relative_to(ROOT)}")
