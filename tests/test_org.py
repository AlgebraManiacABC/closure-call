"""Staff and facility editing: who may edit, validation, the admin log, and that the app uses the edits.

Run from the repo root:  python tests/test_org.py
"""
import os
import secrets
import sys
import tempfile
from pathlib import Path

os.environ["DB"] = f"sqlite:///{(Path(tempfile.mkdtemp()) / 'org.db').as_posix()}"
os.environ["SESSION_SECRET"] = secrets.token_urlsafe(32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging  # noqa: E402

logging.disable(logging.WARNING)

from fastapi.testclient import TestClient  # noqa: E402

from app import facility as fac  # noqa: E402
from app.organization import Organization  # noqa: E402
from app.web import accounts, app, state, store  # noqa: E402

PW = {u: secrets.token_urlsafe(12) for u in ("mgr", "rev", "adm")}  # test-only, generated per run
accounts.add("mgr", "Test Manager", "manager", PW["mgr"])
accounts.add("rev", "Test Reviewer", "reviewer", PW["rev"])
accounts.add("adm", "Test Admin", "sysadmin", PW["adm"])


def client(user):
    c = TestClient(app, follow_redirects=False)
    assert c.post("/login", data={"username": user, "password": PW[user], "next": "/"}).status_code == 303
    return c


def employee_form(**kw):
    f = {"name": "Pat Rivera", "title": "Soil Conservationist", "agency": "NRCS", "phone": "+1-555-0199",
         "email": "pat.rivera@usda.example", "preferred": "email", "shift_start": "08:00", "shift_end": "16:30",
         "telework_eligible": "on", "telework_days": ["Mon", "Fri"], "commute_miles": "12"}
    f.update(kw)
    return f


def test_reviewer_can_read_but_not_edit():
    c = client("rev")
    assert c.get("/organization").status_code == 200
    page = c.get("/organization/employee/E001")
    assert page.status_code == 200 and "Reese Okafor" in page.text and "<fieldset disabled" in page.text
    r = c.post("/organization/employee/new", data=employee_form())
    assert "error=" in r.headers["location"]
    assert not any(e.name == "Pat Rivera" for e in fac.load_roster())


def test_manager_adds_and_edits_an_employee_and_it_is_logged():
    c = client("mgr")
    n = len(fac.load_roster())
    r = c.post("/organization/employee/new", data=employee_form())
    assert "error" not in r.headers["location"], r.headers["location"]
    pat = next(e for e in fac.load_roster() if e.name == "Pat Rivera")
    assert len(fac.load_roster()) == n + 1 and pat.telework_days == ["Mon", "Fri"] and pat.preferred_channel == "email"
    c.post(f"/organization/employee/{pat.id}", data=employee_form(
        essential="on", leave_start=["2026-10-13", ""], leave_end=["2026-10-14", ""],
        detail_location="FSA State Office, Des Moines", detail_start="2026-11-02", detail_end="2026-11-20",
        access_hearing="on", access_hearing_note="Text only"))
    pat = next(e for e in fac.load_roster() if e.id == pat.id)
    assert pat.essential and [(r.start.isoformat(), r.end.isoformat()) for r in pat.planned_leave] == [("2026-10-13", "2026-10-14")]
    assert pat.detail.location.startswith("FSA") and pat.accessibility_needs == [{"type": "hearing", "note": "Text only"}]
    page = c.get(f"/organization/employee/{pat.id}").text
    assert 'value="2026-10-13"' in page and "Text only" in page
    c.post(f"/organization/employee/{pat.id}", data=employee_form())  # clearing the fields clears them
    pat = next(e for e in fac.load_roster() if e.id == pat.id)
    assert not pat.planned_leave and pat.detail is None and not pat.accessibility_needs
    assert any(e for e in state.roster if e.id == pat.id)
    actions = [e["action"] for e in store.audit.entries(20)]
    assert "employee_added" in actions and "employee_changed" in actions


def test_bad_input_is_refused_and_nothing_changes():
    c = client("mgr")
    before = fac.roster_data()
    for bad in (employee_form(email="not-an-email"), employee_form(shift_start="soon"),
                employee_form(leave_start=["2026-10-20"], leave_end=["2026-10-19"]),
                employee_form(detail_location="Des Moines", detail_start="")):
        r = c.post("/organization/employee/new", data=bad)
        assert "error=" in r.headers["location"], bad
    assert fac.roster_data() == before


def test_sysadmin_changes_the_facility_manager_and_hours_and_survives_restart():
    c = client("adm")
    f = fac.facility_data()
    pin = state.facility.pin
    form = {"name": "USDA Service Center", "agencies": "FSA, NRCS", "manager_employee_id": "E002",
            "open": "07:30", "close": "16:00", "operating_days": ["Mon", "Tue", "Wed", "Thu"],
            "morning_start": "06:00", "morning_end": "08:00", "evening_start": "15:30", "evening_end": "17:30",
            "decide_by": "04:30", "closed_date": ["2026-10-15", ""], "closed_name": ["Building maintenance", ""]}
    r = c.post("/organization/facility", data=form)
    assert "error" not in r.headers["location"], r.headers["location"]
    assert state.facility.manager_employee_id == "E002" and state.facility.operating_days == ["Mon", "Tue", "Wed", "Thu"]
    assert state.facility.pin == pin  # editing the profile keeps the current location
    from datetime import date
    assert not state.facility.is_operating_day(date(2026, 10, 15))  # the extra closed day
    assert not state.facility.is_operating_day(date(2026, 11, 26))  # Thanksgiving, computed
    assert state.facility.is_operating_day(date(2026, 10, 14))
    r = client("mgr").post("/organization/employee/E002/remove")
    assert "facility+manager" in r.headers["location"] or "facility%20manager" in r.headers["location"]
    fac.set_override("facility", None)
    Organization(store.engine, store.audit)  # a restart reloads the saved copy
    assert fac.load_facility().manager_employee_id == "E002"


if __name__ == "__main__":
    for k, f in dict(globals()).items():
        if k.startswith("test_"):
            f()
            print("ok", k)
