"""Staff and facility settings edited in the app: the facility profile and the employee roster.

Saved copies live in their own `org_config` table (separate metadata, so erasing the decision record
keeps them) and replace data/facility.json and data/roster.json, which stay as the starting point.
Every save is checked by parsing it the same way the app loads it, and goes in the admin log with
who changed what. Notices already drafted keep the roster they were drafted from.
"""
import json
import re
import threading
from datetime import date, datetime, time, timezone

import sqlalchemy as sa

from . import facility as fac

org_meta = sa.MetaData()
org_config = sa.Table(
    "org_config", org_meta,
    sa.Column("name", sa.String(40), primary_key=True),
    sa.Column("data", sa.Text, nullable=False),
    sa.Column("updated_by", sa.String(160), nullable=False),
    sa.Column("updated_at", sa.String(40), nullable=False),
)
WEEKDAYS = fac.WEEKDAYS
# The needs the notices act on (notify.personal_notes and the hearing-only channel rule).
ACCESSIBILITY = {"mobility": "Mobility (snow or ice on the lot and ramp is a barrier)",
                 "vision": "Vision (relies on a ride in darkness or bad weather)",
                 "hearing": "Deaf or hard of hearing (text and email only, no voice calls)",
                 "medical": "Medical condition worsened by extreme cold"}


class OrgError(ValueError):
    pass


class Organization:
    def __init__(self, engine: sa.Engine, audit):
        self.engine, self.audit = engine, audit
        self._lock = threading.Lock()
        org_meta.create_all(engine)
        self.load()

    def load(self):
        with self.engine.connect() as c:
            rows = c.execute(sa.select(org_config)).all()
        for r in rows:
            fac.set_override(r.name, json.loads(r.data))

    def updated(self) -> dict:
        with self.engine.connect() as c:
            return {r.name: {"by": r.updated_by, "at": r.updated_at}
                    for r in c.execute(sa.select(org_config.c.name, org_config.c.updated_by, org_config.c.updated_at))}

    def _save(self, name: str, data: dict, actor: str, action: str, target: str, details: dict):
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self.engine.begin() as c:
            c.execute(org_config.delete().where(org_config.c.name == name))
            c.execute(org_config.insert().values(name=name, data=json.dumps(data), updated_by=actor, updated_at=at))
        fac.set_override(name, data)
        self.audit.append(action, actor, target, details)

    # ---------- facility ----------

    def save_facility(self, form: dict, actor: str):
        d = json.loads(json.dumps(fac.facility_data()))
        name = form.get("name", "").strip()
        if not name:
            raise OrgError("The facility needs a name")
        d["name"] = name
        d["agencies"] = [a.strip() for a in form.get("agencies", "").split(",") if a.strip()]
        ids = {e["id"] for e in fac.roster_data()["employees"]}
        if form.get("manager_employee_id") not in ids:
            raise OrgError("Pick the facility manager from the roster")
        d["manager_employee_id"] = form["manager_employee_id"]
        d["hours"] = {"open": _hm(form, "open"), "close": _hm(form, "close")}
        if d["hours"]["open"] >= d["hours"]["close"]:
            raise OrgError("Opening time must be before closing time")
        days = [w for w in WEEKDAYS if w in form.get("operating_days", [])]
        if not days:
            raise OrgError("Pick at least one operating day")
        d["operating_days"] = days
        d["commute_windows"] = {"morning": [_hm(form, "morning_start"), _hm(form, "morning_end")],
                                "evening": [_hm(form, "evening_start"), _hm(form, "evening_end")]}
        d["decision_deadlines"] = {**d.get("decision_deadlines", {}), "delay_or_closure_by": _hm(form, "decide_by")}
        hol = []
        for line in re.split(r"[\s,]+", form.get("holidays", "")):
            if line:
                try:
                    hol.append(date.fromisoformat(line).isoformat())
                except ValueError:
                    raise OrgError(f"Holiday '{line}' is not a date like 2026-11-26")
        d["federal_holidays"] = sorted(set(hol))
        _check(lambda: fac.facility_from_dict(d))
        before = fac.facility_data()
        changed = sorted(k for k in d if d.get(k) != before.get(k))
        self._save("facility", d, actor, "facility_changed", d["name"], {"changed": changed})

    # ---------- roster ----------

    def save_employee(self, emp_id: str, form: dict, actor: str) -> str:
        r = json.loads(json.dumps(fac.roster_data()))
        emps = r["employees"]
        if emp_id == "new":
            nums = [int(m.group(1)) for e in emps if (m := re.fullmatch(r"E(\d+)", e["id"]))]
            emp_id = f"E{max(nums, default=0) + 1:03d}"
            e = {"id": emp_id, "duty_station": fac.facility_data()["id"], "detail": None, "planned_leave": [],
                 "accessibility_needs": []}
            emps.append(e)
            action = "employee_added"
        else:
            e = next((x for x in emps if x["id"] == emp_id), None)
            if e is None:
                raise OrgError("No such employee")
            action = "employee_changed"
        before = json.loads(json.dumps(e))
        name = form.get("name", "").strip()
        if not name:
            raise OrgError("Name is required")
        phone, email = form.get("phone", "").strip(), form.get("email", "").strip()
        if not phone or not email or "@" not in email:
            raise OrgError("A phone number and an email address are required")
        e.update({
            "name": name, "title": form.get("title", "").strip(), "agency": form.get("agency", "").strip(),
            "essential": "essential" in form, "telework_eligible": "telework_eligible" in form,
            "telework_days": [w for w in WEEKDAYS if w in form.get("telework_days", [])],
            "shift": {"start": _hm(form, "shift_start"), "end": _hm(form, "shift_end")},
            # Kept for the data file but not on the form: nothing uses miles, and the flood safety line now goes
            # to everyone who commutes during a flood.
            "commute_miles": e.get("commute_miles", 0), "commute_flood_prone": e.get("commute_flood_prone", False),
            "contact": {"phone": phone, "email": email,
                        "preferred": "email" if form.get("preferred") == "email" else "sms"},
            "notes": form.get("notes", "").strip(),
        })
        leave = []
        for s, t in zip(form.get("leave_start", []), form.get("leave_end", [])):
            if s or t:
                s, t = _date(s or t, "Leave start"), _date(t or s, "Leave end")
                if t < s:
                    raise OrgError("A leave period ends before it starts")
                leave.append({"start": s, "end": t})
        e["planned_leave"] = sorted(leave, key=lambda x: x["start"])
        where = form.get("detail_location", "").strip()
        if where:
            s, t = _date(form.get("detail_start", ""), "Detail start"), _date(form.get("detail_end", ""), "Detail end")
            if t < s:
                raise OrgError("The detail ends before it starts")
            e["detail"] = {"location": where, "start": s, "end": t}
        else:
            e["detail"] = None
        known = [a for a in e.get("accessibility_needs", []) if a.get("type") not in ACCESSIBILITY]
        e["accessibility_needs"] = known + [
            {"type": k, "note": form.get(f"access_{k}_note", "").strip()}
            for k in ACCESSIBILITY if f"access_{k}" in form]
        _check(lambda: fac.roster_from_dict(r))
        changed = sorted(k for k in e if e.get(k) != before.get(k))
        self._save("roster", r, actor, action, f"{emp_id} {name}", {"changed": changed})
        return emp_id

    def remove_employee(self, emp_id: str, actor: str):
        r = json.loads(json.dumps(fac.roster_data()))
        if emp_id == fac.facility_data()["manager_employee_id"]:
            raise OrgError("This person is the facility manager; pick a new facility manager first")
        e = next((x for x in r["employees"] if x["id"] == emp_id), None)
        if e is None:
            raise OrgError("No such employee")
        r["employees"].remove(e)
        self._save("roster", r, actor, "employee_removed", f"{emp_id} {e['name']}", {})


def _hm(form: dict, key: str) -> str:
    v = (form.get(key) or "").strip()
    try:
        return time.fromisoformat(v).strftime("%H:%M")
    except ValueError:
        raise OrgError(f"'{key.replace('_', ' ')}' needs a time")


def _date(v: str, what: str) -> str:
    try:
        return date.fromisoformat((v or "").strip()).isoformat()
    except ValueError:
        raise OrgError(f"{what} needs a date")


def _int(form: dict, key: str) -> int:
    try:
        return max(0, int(form.get(key) or 0))
    except ValueError:
        raise OrgError(f"'{key.replace('_', ' ')}' needs a whole number")


def _check(parse):
    try:
        parse()
    except OrgError:
        raise
    except Exception as ex:
        raise OrgError(f"That would not load: {type(ex).__name__}: {ex}")
