"""Decision record checks on a throwaway SQLite database (no network, no model).

Run from the repo root:  python tests/test_record.py
"""
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.agent import AgentReport  # noqa: E402
from app.record import RecordError, Store, ledger  # noqa: E402
from app.rules import evaluate  # noqa: E402
from app.sources.http import Fetched  # noqa: E402
from test_rules import gusts, make  # noqa: E402


def fresh() -> tuple[Store, str]:
    db = Path(tempfile.mkdtemp()) / "t.db"
    store = Store(f"sqlite:///{db.as_posix()}")
    inputs = make(gusts([6, 7], 62))  # 2-hour delay
    rec = evaluate(inputs)
    raw = [Fetched("nws", "https://api.weather.gov/x", datetime.now(timezone.utc), 200, {"a": [1, 2.5, "x"]})]
    ref = store.record_evaluation(inputs, rec, raw)
    return store, ref.id


def test_full_lifecycle_verifies_and_replays():
    store, eid = fresh()
    store.record_agent_report(eid, AgentReport(ran=True, reason="ok", rationale="Because wind."), [])
    store.record_question(eid, "Why?", "Gusts of 62 mph.", "Facility manager")
    store.record_decision(eid, "approve", "Quinn (facility manager)")
    loaded = store.load(eid)
    assert loaded["recommendation"].posture == "delayed_arrival" and loaded["agent_report"].rationale
    assert [e["kind"] for e in loaded["events"]] == ["evaluation", "agent_report", "question", "decision"]
    v = store.verify_chain()
    assert v["ok"] and v["entries"] == 4, v
    r = store.replay(eid)
    assert r["matches"] and r["replayed_posture"] == "delayed_arrival", r
    h = store.history()[0]
    assert h["decision"]["action"] == "approve" and h["agent"] and h["questions"] == 1


def test_override_needs_reason_and_details():
    store, eid = fresh()
    for kwargs in ({"posture": "closure", "reason": "short"}, {"posture": "delayed_arrival", "reason": "x" * 20},
                   {"posture": "sideways", "reason": "x" * 20}):
        try:
            store.record_decision(eid, "override", "Quinn", **kwargs)
            raise AssertionError(f"accepted {kwargs}")
        except RecordError:
            pass
    for same in ({"posture": "delayed_arrival", "delay_hours": 2},):  # identical to the recommendation
        try:
            store.record_decision(eid, "override", "Quinn", reason="x" * 20, **same)
            raise AssertionError("accepted an override equal to the recommendation")
        except RecordError:
            pass
    store.record_decision(eid, "override", "Quinn", posture="delayed_arrival", delay_hours=3,
                          reason="Plows need an extra hour on the county roads.")  # same posture, longer delay: fine
    store.record_decision(eid, "override", "Quinn", posture="closure",
                          reason="County sheriff closed the access road at 5 AM.")
    d = store.history()[0]["decision"]
    assert d["posture"] == "closure" and d["label"].startswith("Closed")


def test_sending_needs_a_package_for_the_latest_decision():
    from app.facility import load_facility, load_roster
    from app.notify import Decided, build_package
    store, eid = fresh()
    try:
        store.record_send(eid, "Quinn")
        raise AssertionError("sent without a decision")
    except RecordError:
        pass
    store.record_decision(eid, "approve", "Quinn")
    loaded = store.load(eid)
    pkg = build_package(load_facility(), load_roster(), loaded["inputs"], loaded["recommendation"],
                        Decided(posture="delayed_arrival", label="x", delay_hours=2), use_agent=False)
    store.record_notification_package(eid, pkg)
    store.record_decision(eid, "override", "Quinn", posture="closure", reason="Roads iced over at 4 AM.")
    try:
        store.record_send(eid, "Quinn")  # package was built for the earlier decision
        raise AssertionError("sent a stale package")
    except RecordError:
        pass
    pkg2 = build_package(load_facility(), load_roster(), loaded["inputs"], loaded["recommendation"],
                         Decided(posture="closure", label="Closed"), use_agent=False)
    store.record_notification_package(eid, pkg2)
    _, msgs = store.record_send(eid, "Quinn")
    assert msgs and all(m.status == "simulated" for m in msgs)
    assert store.verify_chain()["ok"]


def test_rows_cannot_be_updated_or_deleted():
    store, eid = fresh()
    for stmt in (ledger.update().values(posture="normal"), ledger.delete()):
        try:
            with store.engine.begin() as c:
                c.execute(stmt)
            raise AssertionError("append-only trigger did not fire")
        except sa.exc.DatabaseError as e:
            assert "append-only" in str(e)


def test_tampering_is_detected():
    store, eid = fresh()
    store.record_decision(eid, "approve", "Quinn")
    with store.engine.begin() as c:  # an attacker with admin rights drops the trigger and edits history
        c.exec_driver_sql("DROP TRIGGER ledger_no_update")
        row = c.execute(sa.select(ledger).where(ledger.c.kind == "decision")).first()
        e = json.loads(row.entry)
        e["payload"]["posture"] = "normal"
        c.execute(ledger.update().where(ledger.c.seq == row.seq).values(entry=json.dumps(e)))
    v = store.verify_chain()
    assert not v["ok"] and any("edited" in p for p in v["problems"]), v


if __name__ == "__main__":
    for k, f in dict(globals()).items():
        if k.startswith("test_"):
            f()
            print("ok", k)
