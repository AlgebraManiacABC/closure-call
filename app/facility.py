"""Synthetic facility and roster: loading, re-pinning, and who is where on a given day.

This module answers factual questions only: is the facility operating that day, and for each
person is that day an office day, a scheduled telework day, a day on detail elsewhere, or
leave. Deciding who to notify for a posture is the roster filter's job, built on top of this.
"""
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DATA = Path(__file__).resolve().parent.parent / "data"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _hm(s: str) -> time:
    return time.fromisoformat(s)


# ---------- federal holidays ----------

def _nth(year: int, month: int, weekday: int, n: int) -> date:
    """The nth given weekday of a month (n = -1 for the last one). Monday is 0."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def federal_holidays(year: int) -> dict[date, str]:
    """Observed federal holidays for a year, computed from 5 U.S.C. 6103 (the list OPM publishes): a holiday
    on a Saturday is observed the Friday before, and one on a Sunday the Monday after."""
    days = {}
    for y in (year, year + 1):  # Jan 1 on a Saturday is observed on Dec 31 of the year before
        fixed = {date(y, 1, 1): "New Year's Day", date(y, 6, 19): "Juneteenth", date(y, 7, 4): "Independence Day",
                 date(y, 11, 11): "Veterans Day", date(y, 12, 25): "Christmas Day"}
        for d, name in fixed.items():
            d = d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d
            days[d] = name
        days.update({_nth(y, 1, 0, 3): "Birthday of Martin Luther King, Jr.", _nth(y, 2, 0, 3): "Washington's Birthday",
                     _nth(y, 5, 0, -1): "Memorial Day", _nth(y, 9, 0, 1): "Labor Day", _nth(y, 10, 0, 2): "Columbus Day",
                     _nth(y, 11, 3, 4): "Thanksgiving Day"})
    return dict(sorted((d, n) for d, n in days.items() if d.year == year))


def federal_holiday(d: date) -> str | None:
    return federal_holidays(d.year).get(d)


# ---------- facility ----------

@dataclass(frozen=True)
class Pin:
    lat: float
    lon: float
    label: str
    time_zone: str
    flood_gauge_lid: str | None = None  # NWPS gauge to watch; None = nearest with flood stages


@dataclass(frozen=True)
class Facility:
    id: str
    name: str
    agencies: list[str]
    manager_employee_id: str
    pin: Pin
    open: time
    close: time
    operating_days: list[str]
    morning_commute: tuple[time, time]
    evening_commute: tuple[time, time]
    delay_or_closure_by: time
    building: dict
    federal_holidays: frozenset[date]  # facility-specific extra closed days; federal ones come from federal_holiday()

    @property
    def display_name(self) -> str:
        return f"{self.name}, {self.pin.label}"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.pin.time_zone)

    def is_operating_day(self, d: date) -> bool:
        return (WEEKDAYS[d.weekday()] in self.operating_days and federal_holiday(d) is None
                and d not in self.federal_holidays)

    def next_operating_day(self, now: datetime | None = None) -> date:
        """The day a decision is for: today if before opening on an operating day, else the next one."""
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        d = now.date()
        if self.is_operating_day(d) and now.time() < self.open:
            return d
        d += timedelta(days=1)
        while not self.is_operating_day(d):
            d += timedelta(days=1)
        return d

    def at(self, d: date, t: time) -> datetime:
        """A local wall-clock time on day d at the facility, timezone-aware."""
        return datetime.combine(d, t, tzinfo=self.tz)

    def repinned(self, pin: Pin) -> "Facility":
        return replace(self, pin=pin)



def repin_to_point(facility: Facility, lat: float, lon: float) -> Facility:
    """Move the facility to any coordinate, taking the place name and time zone from NWS."""
    from .sources import nws
    info, _ = nws.point(lat, lon)
    label = f"{info.city}, {info.state}" if info.city else f"{lat:.3f}, {lon:.3f}"
    return facility.repinned(Pin(lat, lon, label, info.time_zone))


def _pin(d: dict) -> Pin:
    return Pin(d["lat"], d["lon"], d["label"], d["time_zone"], d.get("flood_gauge_lid"))


# Edited copies saved from the Staff and facility page (app/organization.py) replace the JSON files.
_overrides: dict[str, dict] = {}
FACILITY_FILE, ROSTER_FILE = DATA / "facility.json", DATA / "roster.json"


DEFAULT_ID = json.loads(FACILITY_FILE.read_text(encoding="utf-8"))["id"]


def _key(name: str) -> str:
    """Saved documents are "facility:<id>" and "roster:<id>"; the bare names are the starting facility."""
    return f"{name}:{DEFAULT_ID}" if name in ("facility", "roster") else name


def set_override(name: str, data: dict | None):
    if data is None:
        _overrides.pop(_key(name), None)
    else:
        _overrides[_key(name)] = data


def facility_ids(include_archived: bool = False) -> list[str]:
    """Every facility: the starting one from data/facility.json plus any a sysadmin created."""
    ids = [DEFAULT_ID] + sorted(k.split(":", 1)[1] for k in _overrides
                                if k.startswith("facility:") and k != f"facility:{DEFAULT_ID}")
    return [i for i in ids if include_archived or not facility_data(i).get("archived")]


def facility_data(fid: str | None = None) -> dict:
    fid = fid or DEFAULT_ID
    d = _overrides.get(f"facility:{fid}")
    if d is None:
        if fid != DEFAULT_ID:
            raise KeyError(f"no facility {fid}")
        d = json.loads(FACILITY_FILE.read_text(encoding="utf-8"))
    return d


def roster_data(fid: str | None = None) -> dict:
    fid = fid or DEFAULT_ID
    d = _overrides.get(f"roster:{fid}")
    if d is None:
        if fid != DEFAULT_ID:
            raise KeyError(f"no roster for {fid}")
        d = json.loads(ROSTER_FILE.read_text(encoding="utf-8"))
    return d


def load_facility(path: Path | None = None, fid: str | None = None) -> Facility:
    return facility_from_dict(json.loads(path.read_text(encoding="utf-8")) if path else facility_data(fid))


def facility_from_dict(d: dict) -> Facility:
    cw = d["commute_windows"]
    dl = d["decision_deadlines"]
    return Facility(
        id=d["id"], name=d["name"], agencies=d["agencies"], manager_employee_id=d["manager_employee_id"],
        pin=_pin(d["pin"]),
        open=_hm(d["hours"]["open"]), close=_hm(d["hours"]["close"]),
        operating_days=d["operating_days"],
        morning_commute=(_hm(cw["morning"][0]), _hm(cw["morning"][1])),
        evening_commute=(_hm(cw["evening"][0]), _hm(cw["evening"][1])),
        delay_or_closure_by=_hm(dl["delay_or_closure_by"]),
        building=d["building"],
        # Federal holidays are computed; the old JSON "federal_holidays" list is no longer read.
        federal_holidays=frozenset(date.fromisoformat(x["date"]) for x in d.get("extra_closed_days", [])),
    )


# ---------- roster ----------

@dataclass(frozen=True)
class DateRange:
    start: date
    end: date  # inclusive

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass(frozen=True)
class Detail:
    location: str  # a facility id, or a free-text office name
    range: DateRange


@dataclass(frozen=True)
class Employee:
    id: str
    name: str
    title: str
    agency: str
    duty_station: str
    detail: Detail | None
    essential: bool
    telework_eligible: bool
    telework_days: list[str]
    planned_leave: list[DateRange]
    accessibility_needs: list[dict]
    shift_start: time
    shift_end: time
    commute_miles: int
    commute_flood_prone: bool
    phone: str
    email: str
    preferred_channel: str  # "sms" or "email"
    notes: str = ""


def _range(d: dict) -> DateRange:
    return DateRange(date.fromisoformat(d["start"]), date.fromisoformat(d["end"]))


def load_roster(path: Path | None = None, fid: str | None = None) -> list[Employee]:
    return roster_from_dict(json.loads(path.read_text(encoding="utf-8")) if path else roster_data(fid))


def roster_from_dict(d: dict) -> list[Employee]:
    out = []
    for e in d["employees"]:
        det = e.get("detail")
        out.append(Employee(
            id=e["id"], name=e["name"], title=e["title"], agency=e["agency"], duty_station=e["duty_station"],
            detail=Detail(det["location"], _range(det)) if det else None,
            essential=e["essential"], telework_eligible=e["telework_eligible"],
            telework_days=e["telework_days"], planned_leave=[_range(x) for x in e["planned_leave"]],
            accessibility_needs=e["accessibility_needs"],
            shift_start=_hm(e["shift"]["start"]), shift_end=_hm(e["shift"]["end"]),
            commute_miles=e["commute_miles"], commute_flood_prone=e["commute_flood_prone"],
            phone=e["contact"]["phone"], email=e["contact"]["email"],
            preferred_channel=e["contact"]["preferred"], notes=e.get("notes", ""),
        ))
    return out


# ---------- who is where on a given day ----------

@dataclass(frozen=True)
class DayStatus:
    employee: Employee
    day: date
    attached: bool  # works at this facility that day (assigned and not detailed out, or detailed in)
    location: str  # "office", "telework", "leave", or "detail: <place>"
    reason: str  # plain-language explanation, reused by the roster filter and the decision record
    detailed_in: bool = False
    flags: list[str] = field(default_factory=list)  # essential, accessibility:<type>, flood_prone_commute, ...


def day_status(emp: Employee, facility: Facility, d: date) -> DayStatus:
    dow = WEEKDAYS[d.weekday()]
    detail_active = emp.detail is not None and emp.detail.range.covers(d)
    detailed_in = detail_active and emp.detail.location == facility.id
    detailed_out = detail_active and not detailed_in
    assigned_here = emp.duty_station == facility.id

    flags = []
    if emp.essential:
        flags.append("essential")
    if not emp.telework_eligible:
        flags.append("not_telework_eligible")
    if emp.commute_flood_prone:
        flags.append("flood_prone_commute")
    flags += [f"accessibility:{a['type']}" for a in emp.accessibility_needs]

    def status(attached, location, reason):
        return DayStatus(emp, d, attached, location, reason, detailed_in, flags)

    if detailed_out:
        return status(False, f"detail: {emp.detail.location}",
                      f"On detail to {emp.detail.location} through {emp.detail.range.end:%b %d}.")
    if not (assigned_here or detailed_in):
        return status(False, "elsewhere", f"Assigned to {emp.duty_station}, not on detail here.")
    if any(r.covers(d) for r in emp.planned_leave):
        return status(True, "leave", "On approved leave that day.")
    if dow in emp.telework_days:
        return status(True, "telework", f"Scheduled telework day ({dow}).")
    where = f"On detail here from {emp.duty_station}." if detailed_in else "Scheduled to work in the office."
    return status(True, "office", where)


def roster_for_day(roster: list[Employee], facility: Facility, d: date) -> list[DayStatus]:
    """Everyone with a tie to this facility: its own staff (including those away) and people detailed in."""
    out = []
    for e in roster:
        s = day_status(e, facility, d)
        if s.attached or e.duty_station == facility.id:
            out.append(s)
    return out
