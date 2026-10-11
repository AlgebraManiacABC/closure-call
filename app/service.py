"""Pipeline steps shared by the web app and the monitor: check, ask, decide, build package, send.

Slow model work (agent investigation, message drafting) runs in background threads so pages can
show the engine's result right away. `busy(eid)` tells a page what is still running.
"""
import logging
import os
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone

from .agent import answer_question, investigate
from .collect import collect
from .facility import Facility, Pin, load_facility, load_roster, repin_to_point
from .notify import Decided, build_package
from .record import RecordError, Store
from .rules import POSTURE_RANK, Inputs, evaluate
from .sources import hotspots

log = logging.getLogger("closure_call")

_busy: dict[str, set[str]] = {}  # evaluation id -> {"agent", "package"}
_busy_lock = threading.Lock()
_errors: dict[str, str] = {}


def busy(eid: str) -> set[str]:
    with _busy_lock:
        return set(_busy.get(eid, set()))


def last_error(eid: str) -> str | None:
    return _errors.get(eid)


def _run_bg(eid: str, kind: str, fn):
    with _busy_lock:
        _busy.setdefault(eid, set()).add(kind)

    def wrapper():
        try:
            fn()
        except Exception as e:  # shown on the page; the record stays consistent
            log.exception("%s failed for %s", kind, eid)
            _errors[eid] = f"{kind} failed: {type(e).__name__}: {e}"[:300]
        finally:
            with _busy_lock:
                _busy.get(eid, set()).discard(kind)

    threading.Thread(target=wrapper, daemon=True, name=f"{kind}-{eid[:8]}").start()


# ---------- facility state ----------

class AppState:
    def __init__(self):
        self.facility: Facility = load_facility()
        self.roster = load_roster()
        self.last_seen: dict[tuple, tuple[str, str]] = {}  # monitor memory: (lat, lon, day) -> (posture, eid)

    @property
    def manager(self):
        return next(e for e in self.roster if e.id == self.facility.manager_employee_id)

    def set_point(self, lat: float, lon: float):
        """Pin the facility anywhere in the US; NWS supplies the town name and time zone."""
        self.facility = repin_to_point(load_facility(), lat, lon)

    def restore(self, store: Store):
        """After a restart or redeploy, return to the location of the most recent evaluation."""
        try:
            latest = store.history(limit=1)
            if latest:
                self.facility = facility_for(store.load(latest[0]["evaluation_id"])["inputs"])
        except Exception:
            log.exception("could not restore the last location; using the default")


_hotspot_cache: tuple[float, dict] | None = None
_hotspot_lock = threading.Lock()


def live_hotspots(max_age_s: int = 600) -> dict:
    """Where flood, wind and winter alerts are active right now, cached for 10 minutes."""
    global _hotspot_cache
    with _hotspot_lock:
        if _hotspot_cache and _time.time() - _hotspot_cache[0] < max_age_s:
            return _hotspot_cache[1]
        found = hotspots.find(per_hazard=5)
        _hotspot_cache = (_time.time(), found)
        return found


def facility_for(inputs: Inputs) -> Facility:
    """The facility as it was pinned for a recorded evaluation."""
    f = load_facility()
    label = inputs.facility_label.split(", ", 1)[1] if ", " in inputs.facility_label else inputs.facility_label
    same_as_default = abs(f.pin.lat - inputs.lat) < 1e-4 and abs(f.pin.lon - inputs.lon) < 1e-4
    return f if same_as_default else f.repinned(Pin(inputs.lat, inputs.lon, label, inputs.time_zone))


# ---------- steps ----------

def check(store: Store, facility: Facility, day: date, actor: str = "system", background: bool = True) -> str:
    """Fetch, evaluate and record now; investigate with the agent in the background."""
    inputs, fetched = collect(facility, day)
    rec = evaluate(inputs)
    eid = store.record_evaluation(inputs, rec, fetched, actor=actor).id

    def agent_step():
        report, extra = investigate(inputs, rec)
        store.record_agent_report(eid, report, extra)

    if background:
        _run_bg(eid, "agent", agent_step)
    else:
        agent_step()
    return eid


def investigate_later(store: Store, eid: str):
    """Run the agent for an evaluation that has no report yet (e.g. a monitor run, or a failed attempt)."""
    if "agent" in busy(eid):
        return
    d = store.load(eid)
    _errors.pop(eid, None)
    _run_bg(eid, "agent", lambda: store.record_agent_report(eid, *investigate(d["inputs"], d["recommendation"])))


def ask(store: Store, eid: str, question: str, asked_by: str) -> str:
    d = store.load(eid)
    answer = answer_question(d["inputs"], d["recommendation"], d["agent_report"], question)
    store.record_question(eid, question, answer, asked_by)
    return answer


def start_package(store: Store, eid: str, use_agent: bool = True):
    def step():
        d = store.load(eid)
        dp = store.latest_decision(eid)["payload"]
        decided = Decided(posture=dp["posture"], label=dp["posture_label"], delay_hours=dp["delay_hours"],
                          dismissal_time=dp["dismissal_time"], action=dp["action"])
        pkg = build_package(facility_for(d["inputs"]), load_roster(), d["inputs"], d["recommendation"], decided,
                            use_agent=use_agent)
        store.record_notification_package(eid, pkg)

    store.latest_decision(eid)  # raises if there is no decision yet
    _errors.pop(eid, None)
    _run_bg(eid, "package", step)


# ---------- monitor: prompts the manager only when something changes ----------

PUSH_LABELS = {"normal": "Open", "unscheduled_leave": "Leave or telework allowed", "closure": "Closed"}


def severity(posture: str, delay_hours: int | None = None) -> tuple:
    """Higher is worse: open < leave option < delay or early dismissal < closure; a longer delay is worse."""
    return POSTURE_RANK[posture], delay_hours or 0


def push_label(rec) -> str:
    if rec.posture == "delayed_arrival":
        return f"Delayed arrival, {rec.delay_hours} hours"
    if rec.posture == "early_dismissal":
        return f"Early dismissal at {rec.dismissal_time:%I:%M %p}".replace(" 0", " ")
    return PUSH_LABELS[rec.posture]


def monitor_tick(store: Store, state: AppState, base_url: str, day_mode: str = "next", push=None) -> str | None:
    """One scheduled check. Records an evaluation every time; alerts the manager only when a non-normal
    posture first appears, the posture changes, or the decision deadline is near with no decision.
    day_mode "tomorrow" watches the next calendar day as if open (for weekend demos)."""
    f = state.facility
    day = f.next_operating_day() if day_mode == "next" else datetime.now(f.tz).date() + timedelta(days=1)
    key = (f.pin.lat, f.pin.lon, str(day))  # all pins share one facility id, so key by location
    prev = state.last_seen.get(key)  # (posture, evaluation id, delay hours)
    inputs, fetched = collect(f, day)
    rec = evaluate(inputs)
    eid = store.record_evaluation(inputs, rec, fetched, actor="monitor").id
    state.last_seen[key] = (rec.posture, eid, rec.delay_hours)
    was = severity(prev[0], prev[2]) if prev else severity("normal")
    escalated = severity(rec.posture, rec.delay_hours) > was
    reason = None
    if (prev is None and rec.posture != "normal") or (prev is not None and prev[0] != rec.posture):
        reason = (f"Recommendation changed from {prev[0].replace('_', ' ')} to {rec.posture.replace('_', ' ')}."
                  if prev else "New weather recommendation needs your decision.")
        _run_bg(eid, "agent", lambda: store.record_agent_report(eid, *investigate(inputs, rec)))
    elif rec.posture != "normal" and rec.announce_by and prev:
        # A decision on ANY evaluation for this day counts, e.g. one the manager made from "Check now".
        decided = any(h["day"] == str(day) and h["decision"] for h in store.history(f.id, limit=200))
        if not decided and timedelta(0) < rec.announce_by - datetime.now(timezone.utc) <= timedelta(hours=1):
            reason = f"Decision deadline {rec.announce_by:%H:%M} is within the hour and no decision is recorded."
    if reason:
        manager = state.manager
        body = (f"Closure Call: {f.display_name}, {day:%a %b %d}: {rec.posture_label}. {reason} "
                f"Review: {base_url}/e/{eid}")
        delivery = None
        if escalated and push is not None:  # real phone notifications only when things get worse
            before = push_label_for(prev) if prev else "Open"
            delivery = push.send_to_managers({
                "title": f"Closure Call: {push_label(rec)}",
                "body": f"{f.display_name}, {day:%a %b %d}. Was: {before}. Tap to review and decide.",
                "url": f"/e/{eid}", "tag": f"closure-call-{day}"})
        store.record_manager_alert(eid, reason, manager.name, manager.phone, body, push=delivery)
    return eid if reason else None


def push_label_for(prev: tuple) -> str:
    posture, _, hours = prev
    if posture == "delayed_arrival":
        return f"Delayed arrival, {hours} hours"
    return PUSH_LABELS.get(posture, posture.replace("_", " ").capitalize())


def run_monitor(store: Store, state: AppState, base_url: str, minutes: float, day_mode: str = "next", push=None):
    def loop():
        while True:
            try:
                monitor_tick(store, state, base_url, day_mode, push)
            except Exception:
                log.exception("monitor tick failed")
            _time.sleep(minutes * 60)

    threading.Thread(target=loop, daemon=True, name="monitor").start()
