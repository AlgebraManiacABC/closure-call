"""Notification package checks: roster filter, group facts, personal notes, draft validation (no model).

Run from the repo root:  python tests/test_notify.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.facility import load_facility, load_roster  # noqa: E402
from app.notify import Decided, GroupDraft, _check, build_package, outbox  # noqa: E402
from app.rules import GaugeIn, evaluate  # noqa: E402
from test_rules import gusts, make  # noqa: E402

FAC, ROSTER = load_facility(), load_roster()


def package(posture, inputs=None, **kw):
    inputs = inputs or make(gusts([6, 7], 62))
    rec = evaluate(inputs)
    d = Decided(posture=posture, label=posture, **kw)
    return build_package(FAC, ROSTER, inputs, rec, d, use_agent=False), rec


def by_id(pkg):
    return {r.employee_id: r for r in pkg.recipients}


def test_closure_notifies_everyone_present_and_skips_away_and_manager():
    pkg, _ = package("closure")
    r = by_id(pkg)
    assert not r["E001"].notify and "manager" in r["E001"].reasons[0]
    assert not r["E006"].notify and "detail" in r["E006"].reasons[0].lower()  # detailed out
    assert not r["E012"].notify and "leave" in r["E012"].reasons[0]
    assert r["E008"].notify and any("Detailed in" in x for x in r["E008"].reasons)  # detailed in
    assert r["E002"].group == "essential" and r["E003"].group == "essential"
    assert r["E013"].group == "office_no_telework"
    facts = {g.group: g.facts for g in pkg.groups}
    assert "weather and safety leave" in facts["office_no_telework"]
    assert "work from home" in facts["office_telework_ready"]
    assert all(set(x.channels) == {"sms", "email"} for x in pkg.recipients if x.notify)  # urgent


def test_delay_gives_early_shift_its_own_start_time():
    pkg, _ = package("delayed_arrival", delay_hours=2)
    note = " ".join(by_id(pkg)["E019"].personal_notes)
    assert "6:30 AM" in note and "8:30 AM" in note


def test_early_dismissal_skips_teleworkers():
    pkg, _ = package("early_dismissal", dismissal_time="13:00")
    tele = [r for r in pkg.recipients if r.group == "teleworking"]
    assert tele and not any(r.notify for r in tele)
    assert any("1:00 PM" in g.facts for g in pkg.groups)


def test_unscheduled_leave_is_not_urgent_and_skips_essential():
    pkg, _ = package("unscheduled_leave", inputs=make(gusts(range(9, 12), 50)))
    r = by_id(pkg)
    assert not r["E002"].notify  # essential: no change
    plain = [x for x in pkg.recipients if x.notify and not x.personal_notes]
    assert plain and all(len(x.channels) == 1 for x in plain)  # preferred channel only


def test_flood_prone_commuters_get_flood_note():
    g = GaugeIn(lid="X", name="Test River", distance_mi=1, stages_ft={"moderate": 10, "major": 12}, impacts=[],
                latest_stage_ft=13, latest_time=None, crest_stage_ft=None, crest_time=None, forecast_issued=None)
    inp = make(gauge=g)
    inp = inp.model_copy(update={"hours": [h.model_copy(update={"gauge_stage_ft": 13.0}) for h in inp.hours]})
    pkg, rec = package("closure", inputs=inp)
    assert rec.posture == "closure"
    pkg, _ = package("unscheduled_leave", inputs=inp)
    notes = " ".join(by_id(pkg)["E017"].personal_notes)
    assert "Turn Around" in notes and pkg.reason_phrase == "flooding on the Test River"


def test_draft_checks_catch_missing_facts():
    d = Decided(posture="delayed_arrival", label="x", delay_hours=2)
    day = date(2026, 10, 13)
    assert _check("Service Center, Tuesday: 2-hour delay.", d, day) == []
    assert "missing the delay length" in _check("Service Center, Tuesday: arrive late.", d, day)
    assert "missing the day" in _check("Service Center: 2-hour delay.", d, day)
    assert any("too long" in p for p in _check("Tuesday 2-hour delay " + "x" * 400, d, day, 320))


def test_outbox_one_message_per_channel_with_notes_first_in_email():
    pkg, _ = package("delayed_arrival", delay_hours=2)
    msgs = outbox(pkg)
    expected = sum(len(r.channels) for r in pkg.recipients if r.notify)
    assert len(msgs) == expected
    e013 = next(m for m in msgs if m.employee_id == "E019" and m.channel == "email")
    assert e013.body.split("\n\n")[1].startswith("For you specifically")


if __name__ == "__main__":
    for k, f in dict(globals()).items():
        if k.startswith("test_"):
            f()
            print("ok", k)
