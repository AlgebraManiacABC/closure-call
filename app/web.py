"""Closure Call web app: manager decision page, message outbox, decision history.

Run locally:   uvicorn app.web:app --reload        (add DB=local to use the SQLite file)
No login for the demo: a role switcher (cookie) picks manager or reviewer.
"""
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import service
from .record import RecordError, get_store
from .rules import HOUR
from .sources import geocode
from .sources.http import SourceError

logging.basicConfig(level=logging.INFO)
HERE = Path(__file__).resolve().parent
app = FastAPI(title="Closure Call")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
store = get_store(os.environ.get("DB") or None)
state = service.AppState()
state.restore(store)  # survive restarts and redeploys at the last evaluated location
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

POSTURE_NAMES = {"normal": "Normal operations", "unscheduled_leave": "Unscheduled leave / telework",
                 "delayed_arrival": "Delayed arrival", "early_dismissal": "Early dismissal", "closure": "Closure"}


# ---------- template helpers ----------

def _dt(v) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def fmt_time(v, tz: str | None = None, fmt: str = "%a %b %d, %I:%M %p") -> str:
    try:
        d = _dt(v)
        if d is None or d.year < 1900:  # NWPS uses 0001-01-01 for "no forecast"
            return "-"
        if tz and d.tzinfo:
            d = d.astimezone(ZoneInfo(tz))
        return re.sub(r"(^|\s)0(\d)", r"\1\2", d.strftime(fmt))  # 06:00 PM -> 6:00 PM, Oct 05 -> Oct 5
    except (ValueError, OverflowError):
        return str(v)


templates.env.filters["t"] = fmt_time
templates.env.filters["friendly"] = lambda s: friendly(s)
templates.env.globals.update(POSTURE_NAMES=POSTURE_NAMES, HOUR=HOUR,
                             CSS_VERSION=int((HERE / "static" / "style.css").stat().st_mtime),
                             DID={"approve": "Approved", "override": "Overridden"})


RULE_NAMES = {  # display names only; records keep the rule ids so replays match
    "WIND-GUST-WARN": "Wind gusts, warning level", "WIND-SUST-WARN": "Sustained wind, warning level",
    "WIND-GUST-ADV": "Wind gusts, advisory level", "WIND-SUST-ADV": "Sustained wind, advisory level",
    "SNOW-WARN": "Snow in 12 hours, warning level", "SNOW-ADV": "Snow in 12 hours, advisory level",
    "ICE-WARN": "Ice buildup, warning level", "ICE-ADV": "Any freezing rain, advisory level",
    "COLD-WARN": "Wind chill, warning level", "COLD-ADV": "Wind chill, advisory level",
    "FLOOD-GAUGE-MAJOR": "River at major flood stage", "FLOOD-GAUGE-MODERATE": "River at moderate flood stage",
    "WIND-ALERT-WARN": "NWS high wind warning", "WIND-ALERT-ADV": "NWS wind advisory",
    "WINTER-ALERT-WARN": "NWS winter storm, blizzard or ice storm warning", "WINTER-ALERT-ADV": "NWS winter weather advisory",
    "COLD-ALERT-WARN": "NWS extreme cold warning", "COLD-ALERT-ADV": "NWS cold weather advisory",
    "FLOOD-ALERT-FFW": "NWS flash flood warning", "FLOOD-ALERT-WARN": "NWS flood warning or advisory",
    "TIME-DELAY": "Delay past the hazard", "TIME-CLOSE-PERSISTS": "Hazard lasts too long for a delay",
    "TIME-CLOSE-EARLY-HAZARD": "Hazard arrives before noon", "TIME-EARLY-DISMISS": "Dismiss before the hazard",
}
_RULE_RE = re.compile("|".join(sorted(map(re.escape, RULE_NAMES), key=len, reverse=True)))


def friendly(text: str) -> str:
    """Replace rule ids inside engine-written text with their plain names."""
    return _RULE_RE.sub(lambda m: RULE_NAMES[m.group(0)].lower(), str(text))


templates.env.globals["RULE_NAMES"] = RULE_NAMES


FIELD_TITLES = {"gust_mph": "Wind gusts of {v:g} mph", "sustained_mph": "Sustained wind of {v:g} mph",
                "snow_storm_in": "{v:g} in of snow in 12 hours", "ice_storm_in": "{v:g} in of ice",
                "wind_chill_f": "Wind chill of {v:g}°F", "gauge_stage_held_ft": "River at {v:g} ft"}
LEVEL_NAMES = {"FLOOD-GAUGE-MAJOR": "major flood stage", "FLOOD-GAUGE-MODERATE": "moderate flood stage"}


def short_posture(posture: str, delay_hours=None, dismissal=None, tz=None) -> str:
    """The posture in a few words, for headlines; the full OPM wording is shown smaller."""
    if posture == "delayed_arrival":
        return f"Delayed arrival · {delay_hours} hours"
    if posture == "early_dismissal":
        if isinstance(dismissal, str) and len(dismissal) == 5:  # "HH:MM" as recorded on a decision
            dismissal = datetime.strptime(dismissal, "%H:%M")
            tz = None
        return f"Early dismissal · {fmt_time(dismissal, tz, '%I:%M %p')}"
    return {"normal": "Open", "unscheduled_leave": "Open · leave or telework allowed", "closure": "Closed"}[posture]


def reason_rows(findings, inputs, evidence) -> list[dict]:
    """One row per finding: a one-line title and when, with the details behind it."""
    rows = []
    for f in findings:
        if f.kind == "alert":
            row = {"title": f"NWS {f.alert_event}", "sub": f"NWS rates it {f.alert_certainty.lower()}",
                   "quotes": [e for e in evidence if e.alert_id == f.alert_id], "alert_id": f.alert_id}
        else:
            level = LEVEL_NAMES.get(f.rule_id, "warning level" if f.level == 2 else "advisory level")
            row = {"title": FIELD_TITLES.get(f.field, f.rule_id).format(v=f.value),
                   "sub": f"{level} is {f.threshold:g} {f.unit.split()[0]}, so {abs(f.margin):g} "
                          f"{'over' if f.margin >= 0 else 'under'}", "quotes": [], "alert_id": None}
        src = inputs.sources[f.source_key]
        row.update(f=f, source=src.label, fetched=src.fetched_at, issued=src.issued,
                   threshold_source=f.threshold_source)
        rows.append(row)
    return rows


def role(request: Request) -> str:
    return request.cookies.get("role", "manager")


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"role": role(request), "state": state,
                                                      "error": request.query_params.get("error"), **ctx})


def back(url: str, error: str | None = None) -> RedirectResponse:
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url, status_code=303)


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return back("/history" if role(request) == "reviewer" else "/manager")


@app.get("/role/{name}")
def set_role(name: str, request: Request):
    resp = back(request.headers.get("referer") or "/")
    resp.set_cookie("role", name if name in ("manager", "reviewer") else "manager", max_age=86400 * 7)
    return resp


@app.get("/manager", response_class=HTMLResponse)
def manager_home(request: Request):
    f = state.facility
    recent = store.history(f.id, limit=8)
    alerts = [e for e in store.outbox(30) if e["kind"] == "manager_alert"][:3]
    today = datetime.now(f.tz).date()
    return render(request, "manager.html", facility=f, recent=recent, alerts=alerts,
                  next_day=f.next_operating_day(), tomorrow=today + timedelta(days=1), today=today)


HAZARD_NAMES = {"flood": "Flooding", "wind": "High wind", "winter": "Winter weather"}


@app.get("/locations", response_class=HTMLResponse)
def locations(request: Request, q: str = ""):
    results, search_error = [], None
    if q.strip():
        try:
            results = geocode.search(q.strip()[:120])
        except SourceError as e:
            search_error = f"Search is unavailable right now ({e})"[:200]
    try:
        spots = service.live_hotspots()
    except SourceError:
        spots = {}
    markers = [{"hazard": h, "event": s.event, "area": s.area_desc, "lat": s.lat, "lon": s.lon,
                "severity": s.severity} for h, lst in spots.items() for s in lst]
    return render(request, "locations.html", facility=state.facility, q=q, results=results,
                  search_error=search_error, spots=spots, markers=markers, HAZARD_NAMES=HAZARD_NAMES)


@app.post("/pin")
def pin(lat: float = Form(...), lon: float = Form(...)):
    if not (17 <= lat <= 72 and -180 <= lon <= -64):
        return back("/locations", "Pick a point in the United States")
    try:
        state.set_point(round(lat, 4), round(lon, 4))
    except SourceError:
        return back("/locations", "NWS has no forecast for that point; pick a point on US land")
    return back("/manager")


@app.post("/check")
def check_now(day: str = Form("next")):
    f = state.facility
    today = datetime.now(f.tz).date()
    try:
        d = {"next": f.next_operating_day(), "today": today, "tomorrow": today + timedelta(days=1)}.get(day) \
            or date.fromisoformat(day)
        eid = service.check(store, f, d, actor=f"{state.manager.name} (check now)")
    except Exception as e:
        return back("/manager", f"Check failed: {type(e).__name__}: {e}"[:300])
    return back(f"/e/{eid}")


@app.get("/e/{eid}", response_class=HTMLResponse)
def evaluation_page(eid: str, request: Request):
    try:
        d = store.load(eid)
    except RecordError:
        return back("/history", "No such evaluation")
    inputs, rec = d["inputs"], d["recommendation"]
    decision = d["decisions"][-1] if d["decisions"] else None
    package = next((p for p in reversed(d["packages"]) if decision and p["seq"] > decision["seq"]), None)
    send = next((s for s in reversed(d["sends"]) if package and s["seq"] > package["seq"]), None)
    replay = store.replay(eid) if request.query_params.get("replay") else None
    report = d["agent_report"]
    evidence = report.evidence if report else []
    reasons = reason_rows(rec.decisive, inputs, evidence)
    decisive_keys = {(f.rule_id, f.alert_id) for f in rec.decisive}
    also = reason_rows([f for f in rec.findings if (f.rule_id, f.alert_id) not in decisive_keys], inputs, evidence)
    shown_alerts = {r["alert_id"] for r in reasons + also}
    other_quotes = [e for e in evidence if e.alert_id not in shown_alerts]
    headline = (report.headline if report and report.headline else rec.timing_explanation)
    decided = None
    if decision:
        p = decision["payload"]
        decided = short_posture(p["posture"], p.get("delay_hours"), p.get("dismissal_time"), inputs.time_zone)
    return render(request, "evaluation.html", eid=eid, d=d, inputs=inputs, rec=rec, report=report,
                  reasons=reasons, also=also, other_quotes=other_quotes, headline=headline,
                  short=short_posture(rec.posture, rec.delay_hours, rec.dismissal_time, inputs.time_zone),
                  decided_short=decided,
                  decision=decision, package=package["payload"]["package"] if package else None, send=send,
                  busy=service.busy(eid), bg_error=service.last_error(eid), replay=replay, tz=inputs.time_zone,
                  manager=state.manager, raw=d["evaluation"]["payload"]["raw_fetches"])


@app.get("/e/{eid}/status")
def evaluation_status(eid: str):
    """Polled by the decision page while background work runs, so the page reloads only when it finishes."""
    return {"busy": sorted(service.busy(eid)), "error": service.last_error(eid)}


@app.post("/e/{eid}/ask")
def ask(eid: str, request: Request, question: str = Form(...)):
    who = f"{state.manager.name} (facility manager)" if role(request) == "manager" else "Reviewer"
    if question.strip():
        service.ask(store, eid, question.strip()[:500], who)
    return back(f"/e/{eid}#qa")


@app.post("/e/{eid}/decide")
def decide(eid: str, action: str = Form(...), posture: str = Form(""), delay_hours: str = Form(""),
           dismissal_time: str = Form(""), reason: str = Form("")):
    try:
        store.record_decision(eid, action, f"{state.manager.name} (facility manager)", reason,
                              posture or None, int(delay_hours) if delay_hours else None, dismissal_time or None)
    except (RecordError, ValueError) as e:
        return back(f"/e/{eid}#decide", str(e))
    return back(f"/e/{eid}#notify")


@app.post("/e/{eid}/package")
def package(eid: str):
    try:
        service.start_package(store, eid)
    except RecordError as e:
        return back(f"/e/{eid}#notify", str(e))
    return back(f"/e/{eid}#notify")


@app.post("/e/{eid}/send")
def send(eid: str):
    try:
        store.record_send(eid, f"{state.manager.name} (facility manager)")
    except RecordError as e:
        return back(f"/e/{eid}#notify", str(e))
    return back(f"/outbox?e={eid}")


@app.get("/outbox", response_class=HTMLResponse)
def outbox_page(request: Request, e: str | None = None):
    entries = store.outbox()
    if e:
        entries = [x for x in entries if x["evaluation_id"] == e]
    return render(request, "outbox.html", entries=entries, only=e, tz=state.facility.pin.time_zone)


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    verify = store.verify_chain() if request.query_params.get("verify") else None
    return render(request, "history.html", rows=store.history(limit=100), verify=verify,
                  tz=state.facility.pin.time_zone)


@app.get("/raw/{fetch_id}")
def raw(fetch_id: str):
    try:
        return JSONResponse(store.raw_body(fetch_id))
    except RecordError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.get("/healthz")
def healthz():
    return {"ok": True, "db": store.dialect}


@app.on_event("startup")
def start_monitor():
    minutes = float(os.environ.get("MONITOR_MINUTES", "0") or 0)
    if minutes > 0:
        service.run_monitor(store, state, BASE_URL, minutes, os.environ.get("MONITOR_DAY", "next"))
