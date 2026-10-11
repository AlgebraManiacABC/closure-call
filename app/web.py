"""Closure Call web app: manager decision page, message outbox, decision history.

Run locally:   python scripts/serve.py --db local
Every page needs a signed-in account (see app/auth.py and scripts/users.py); managers can act,
reviewers can read, verify, replay and ask. Each ledger entry records who did it.
"""
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from markupsafe import Markup, escape
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, service
from . import push as push_mod
from .record import RecordError, get_store
from .facility import WEEKDAYS, facility_data, federal_holidays, roster_data
from .facility import load_facility as load_facility_config
from .facility import load_roster as load_roster_config
from .organization import ACCESSIBILITY, Organization, OrgError
from .rules import HOUR, load_thresholds
from .sources import geocode
from .sources.http import SourceError

logging.basicConfig(level=logging.INFO)
HERE = Path(__file__).resolve().parent
app = FastAPI(title="Closure Call")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
store = get_store(os.environ.get("DB") or None)
org = Organization(store.engine, store.audit)  # load saved staff and facility edits before anything reads them
state = service.AppState()
state.restore(store)  # survive restarts and redeploys at the last evaluated location
accounts = auth.Accounts(store.engine, store.audit)
pusher = push_mod.Push(store.engine)
PUBLIC_PATHS = ("/login", "/healthz", "/static/", "/sw.js")
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
    "TROPICAL-HURRICANE-WARN": "NWS hurricane or extreme wind warning", "TROPICAL-STORM-WARN": "NWS tropical storm warning",
    "TROPICAL-SURGE-WARN": "NWS storm surge warning",
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


_ELEV_RE = re.compile(r"\b(above|below|over|under)\s+(\d{1,2},\d{3}|\d{3,5})\s*(?:feet|ft)\b", re.I)
# Which forecast check best shows "what is expected here" for each hazard's alerts.
_LOCAL_FIELD = {"wind": "gust_mph", "tropical": "gust_mph", "winter": "snow_storm_in", "flood": "gauge_stage_held_ft"}
_LOCAL_NAME = {"gust_mph": "wind gusts", "snow_storm_in": "snow in 12 hours", "gauge_stage_held_ft": "the river"}


def applies_here(alert, inputs, rec) -> list[str]:
    """Plain-code answers to "does this alert apply to us?", from the recorded data only."""
    notes = [f"NWS returned this alert for the facility's exact location ({inputs.lat:.3f}, {inputs.lon:.3f})."]
    elev = inputs.elevation_ft
    seen = set()
    for word, num in _ELEV_RE.findall(f"{alert.headline}\n{alert.description}"):
        limit = int(num.replace(",", ""))
        up = word.lower() in ("above", "over")
        if (up, limit) in seen:
            continue
        seen.add((up, limit))
        side = "above" if up else "below"
        if elev is None:
            notes.append(f"Part of this alert is for elevations {side} {limit:,} ft; the facility's elevation is not recorded.")
        else:
            inside = elev >= limit if up else elev <= limit
            notes.append(f"Part of this alert is for elevations {side} {limit:,} ft. The facility is at {elev:,.0f} ft, "
                         f"so that part {'applies' if inside else 'does not apply'} here.")
    field = _LOCAL_FIELD.get(alert.hazard)
    checks = [c for c in rec.checks if c.field == field and c.peak is not None]
    if checks:
        worst = max(checks, key=lambda c: (c.crossed, c.level))
        level = "warning" if any(c.crossed and c.level == 2 for c in checks) else \
                "advisory" if any(c.crossed for c in checks) else None
        unit = worst.unit.split(" ")[0]
        notes.append(f"The forecast for the facility itself peaks at {worst.peak:g} {unit} of {_LOCAL_NAME[field]}: "
                     + (f"{level} level." if level else "below advisory level, so the alert covers a wider area than "
                        "the forecast at this exact point."))
    return notes


# NWS alert certainty in plain words (CAP definitions: Observed = happening or has happened; Likely = more
# than about 50% chance; Possible = 50% or less; Unlikely = not expected).
CERTAINTY = {"Observed": "NWS says it is happening now",
             "Likely": "NWS says it is likely (more than a 50% chance)",
             "Possible": "NWS says it is possible (a 50% chance or less)",
             "Unlikely": "NWS says it is not expected",
             "Unknown": "NWS has not rated how certain it is"}


def reason_rows(findings, inputs, evidence, rec=None) -> list[dict]:
    """One row per finding: a one-line title and when, with the details behind it."""
    rows = []
    for f in findings:
        if f.kind == "alert":
            alert = next((a for a in inputs.alerts if a.id == f.alert_id), None)
            row = {"title": f"NWS {f.alert_event}", "sub": CERTAINTY.get(f.alert_certainty, CERTAINTY["Unknown"]),
                   "hazard": f.hazard, "applies": applies_here(alert, inputs, rec) if alert and rec else [],
                   "quotes": [e for e in evidence if e.alert_id == f.alert_id], "alert_id": f.alert_id}
        else:
            level = LEVEL_NAMES.get(f.rule_id, "warning level" if f.level == 2 else "advisory level")
            row = {"title": FIELD_TITLES.get(f.field, f.rule_id).format(v=f.value), "hazard": f.hazard, "applies": [],
                   "sub": f"{level} is {f.threshold:g} {f.unit.split()[0]}, so {abs(f.margin):g} "
                          f"{'over' if f.margin >= 0 else 'under'}", "quotes": [], "alert_id": None}
        src = inputs.sources[f.source_key]
        row.update(f=f, source=src.label, fetched=src.fetched_at, issued=src.issued,
                   threshold_source=f.threshold_source)
        rows.append(row)
    return rows


def me(request: Request) -> auth.User:
    return request.state.user


def role(request: Request) -> str:
    return me(request).role


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(request, name, {"role": user.role if user else None, "user": user,
                                                      "state": state, "error": request.query_params.get("error"),
                                                      **ctx})


def back(url: str, error: str | None = None) -> RedirectResponse:
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url, status_code=303)


def managers_only(request: Request, url: str) -> RedirectResponse | None:
    """Reviewers and sysadmins can read and ask, but only managers act on decisions."""
    return None if me(request).is_manager else back(url, "Only a facility manager account can do that")


def sysadmins_only(request: Request) -> RedirectResponse | None:
    return None if me(request).is_sysadmin else back("/", "Only a sysadmin account can do that")


# ---------- sign-in ----------

@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC_PATHS):
        request.state.user = None
        return await call_next(request)
    user = auth.read_session(request.cookies.get(auth.SESSION_COOKIE), accounts)
    if user is None:
        if path.endswith("/status") or path.startswith("/raw/"):
            return JSONResponse({"error": "signed out"}, status_code=401)
        nxt = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?next=" + quote(nxt), status_code=303)
    request.state.user = user
    return await call_next(request)


def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=_safe_next(next), no_accounts=accounts.count() == 0)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    ip = request.client.host if request.client else "?"
    keys = (f"user:{username.strip().lower()}", f"ip:{ip}")
    retry = "/login?next=" + quote(_safe_next(next))
    if any(auth.failed_logins.blocked(k) for k in keys):
        return back(retry, "Too many failed attempts. Wait 5 minutes and try again.")
    user = accounts.authenticate(username, password)
    if user is None:
        for k in keys:
            auth.failed_logins.hit(k)
        return back(retry, "Wrong username or password")
    for k in keys:
        auth.failed_logins.clear(k)
    resp = back(_safe_next(next))
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(user), max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return resp


@app.post("/logout")
def logout():
    resp = back("/login")
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    u = me(request)
    return back("/manager" if u.is_manager else "/admin" if u.is_sysadmin else "/history")


# ---------- my account ----------

@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    return render(request, "account.html", ROLE_DESCRIPTIONS=auth.ROLE_DESCRIPTIONS,
                  devices=pusher.devices(me(request).username), vapid_public_key=push_mod.public_key(),
                  push_ready=push_mod.configured(), pushed=request.query_params.get("pushed"))


@app.get("/sw.js")
def service_worker():
    """Served from the root so it may handle notifications for the whole site."""
    return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.post("/push/subscribe")
async def push_subscribe(request: Request):
    try:
        pusher.subscribe(me(request).username, await request.json(), request.headers.get("user-agent", ""))
    except (ValueError, TypeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True}


@app.post("/push/remove")
def push_remove(request: Request, sub_id: str = Form(...)):
    pusher.remove(me(request).username, sub_id)
    return back("/account#alerts")


@app.post("/push/test")
def push_test(request: Request):
    r = pusher.send_to_user(me(request).username, {
        "title": "Closure Call: test alert",
        "body": "Phone alerts work. You will be notified when a recommendation escalates.",
        "url": "/account", "tag": "closure-call-test"})
    return back(f"/account?pushed={r['sent']}#alerts")


@app.post("/account/password")
def change_my_password(request: Request, current: str = Form(...), new: str = Form(...), repeat: str = Form(...)):
    u = me(request)
    if accounts.authenticate(u.username, current) is None:
        return back("/account", "Current password is wrong")
    if new != repeat:
        return back("/account", "The new passwords do not match")
    try:
        accounts.set_password(u.username, new, actor=u.actor)
    except auth.AuthError as e:
        return back("/account", str(e))
    # The password fingerprint changed, so issue a fresh session for this browser.
    resp = back("/account?done=1")
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(accounts.get(u.username)),
                    max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https")
    return resp


# ---------- sysadmin ----------

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if (denied := sysadmins_only(request)):
        return denied
    verify = store.audit.verify() if request.query_params.get("verify") else None
    return render(request, "admin.html", users=accounts.list(), counts=store.counts(), audit=store.audit.entries(100),
                  audit_verify=verify, ROLES=auth.ROLES, ROLE_DESCRIPTIONS=auth.ROLE_DESCRIPTIONS,
                  tz=state.facility.pin.time_zone)


@app.post("/admin/users")
def admin_add_user(request: Request, username: str = Form(...), display_name: str = Form(...),
                   role_name: str = Form(...), password: str = Form(...)):
    if (denied := sysadmins_only(request)):
        return denied
    try:
        accounts.add(username, display_name, role_name, password, actor=me(request).actor)
    except auth.AuthError as e:
        return back("/admin#accounts", str(e))
    return back("/admin#accounts")


@app.post("/admin/users/{username}/disabled")
def admin_set_disabled(username: str, request: Request, disabled: str = Form(...)):
    if (denied := sysadmins_only(request)):
        return denied
    if username == me(request).username:
        return back("/admin#accounts", "You cannot disable your own account")
    try:
        accounts.set_disabled(username, disabled == "1", actor=me(request).actor)
    except auth.AuthError as e:
        return back("/admin#accounts", str(e))
    return back("/admin#accounts")


@app.post("/admin/users/{username}/password")
def admin_reset_password(username: str, request: Request, password: str = Form(...)):
    if (denied := sysadmins_only(request)):
        return denied
    try:
        accounts.set_password(username, password, actor=me(request).actor)
    except auth.AuthError as e:
        return back("/admin#accounts", str(e))
    return back("/admin#accounts")


@app.post("/admin/erase")
def admin_erase(request: Request, confirm: str = Form(...), reason: str = Form(...), password: str = Form(...)):
    if (denied := sysadmins_only(request)):
        return denied
    u = me(request)
    if confirm.strip() != "ERASE":
        return back("/admin#erase", "Type ERASE to confirm")
    if accounts.authenticate(u.username, password) is None:
        return back("/admin#erase", "Your password is wrong; nothing was erased")
    try:
        store.erase_all(u.actor, reason)
    except RecordError as e:
        return back("/admin#erase", str(e))
    return back("/history")


def pending_alerts(facility_id: str, limit: int = 3) -> list[dict]:
    """Alerts to the manager that still need a decision. An alert drops off once its evaluation is
    decided, or once any decision for the same day is recorded after it (e.g. after a fresh check)."""
    decided_eids, decided_days = set(), {}
    for h in store.history(facility_id, limit=200):
        if h["decision"]:
            decided_eids.add(h["evaluation_id"])
            decided_days[h["day"]] = max(decided_days.get(h["day"], ""), h["decision"]["at"])
    out = []
    for a in store.outbox(100):
        if a["kind"] != "manager_alert" or a["evaluation_id"] in decided_eids:
            continue
        if decided_days.get(a["day"], "") > a["at"]:
            continue
        out.append(a)
    return out[:limit]


_URL_RE = re.compile(r"https?://[^\s<]+")


def linkify(text: str) -> Markup:
    """Escape text, then turn http(s) URLs into links (used for the simulated texts)."""
    return Markup(_URL_RE.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', str(escape(text))))


templates.env.filters["linkify"] = linkify


@app.get("/manager", response_class=HTMLResponse)
def manager_home(request: Request):
    f = state.facility
    recent = store.history(f.id, limit=8)
    alerts = pending_alerts(f.id)
    today = datetime.now(f.tz).date()
    return render(request, "manager.html", facility=f, recent=recent, alerts=alerts,
                  next_day=f.next_operating_day(), tomorrow=today + timedelta(days=1), today=today)


HAZARD_NAMES = {"flood": "Flooding", "wind": "High wind", "winter": "Winter weather",
                "tropical": "Hurricanes and tropical storms"}


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
def pin(request: Request, lat: float = Form(...), lon: float = Form(...)):
    if (denied := managers_only(request, "/locations")):
        return denied
    if not (17 <= lat <= 72 and -180 <= lon <= -64):
        return back("/locations", "Pick a point in the United States")
    try:
        state.set_point(round(lat, 4), round(lon, 4))
    except SourceError:
        return back("/locations", "NWS has no forecast for that point; pick a point on US land")
    return back("/manager")


@app.post("/check")
def check_now(request: Request, day: str = Form("next")):
    if (denied := managers_only(request, "/manager")):
        return denied
    if not auth.model_actions.hit(me(request).username):
        return back("/manager", "Too many checks and questions in the last 10 minutes; please wait a few minutes")
    f = state.facility
    today = datetime.now(f.tz).date()
    try:
        d = {"next": f.next_operating_day(), "today": today, "tomorrow": today + timedelta(days=1)}.get(day) \
            or date.fromisoformat(day)
        eid = service.check(store, f, d, actor=me(request).actor)
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
    reasons = reason_rows(rec.decisive, inputs, evidence, rec)
    decisive_keys = {(f.rule_id, f.alert_id) for f in rec.decisive}
    also = reason_rows([f for f in rec.findings if (f.rule_id, f.alert_id) not in decisive_keys], inputs, evidence, rec)
    shown_alerts = {r["alert_id"] for r in reasons + also}
    other_quotes = [e for e in evidence if e.alert_id not in shown_alerts]
    decided = None
    if decision:
        p = decision["payload"]
        decided = short_posture(p["posture"], p.get("delay_hours"), p.get("dismissal_time"), inputs.time_zone)
    return render(request, "evaluation.html", eid=eid, d=d, inputs=inputs, rec=rec, report=report,
                  reasons=reasons, also=also, other_quotes=other_quotes,
                  short=short_posture(rec.posture, rec.delay_hours, rec.dismissal_time, inputs.time_zone),
                  decided_short=decided,
                  decision=decision, package=package["payload"]["package"] if package else None, send=send,
                  busy=service.busy(eid), bg_error=service.last_error(eid), replay=replay, tz=inputs.time_zone,
                  manager=state.manager, raw=d["evaluation"]["payload"]["raw_fetches"])


@app.get("/thresholds", response_class=HTMLResponse)
def thresholds_page(request: Request):
    """Every cutoff the rules use, with its source: the brief's 'threshold defensibility' deliverable."""
    th, version = load_thresholds()
    groups = {}
    for rid, r in th["field_rules"].items():
        groups.setdefault(r["hazard"], []).append({"id": rid, "name": RULE_NAMES.get(rid, rid), "level": r["level"],
                                                   "value": r["value"], "unit": r["unit"], "kind": r.get("kind", ""),
                                                   "source": _fill(r["source"], th)})
    alerts = [{"id": rid, "name": RULE_NAMES.get(rid, rid), "level": r["level"], "events": r["events"], "hazard": r["hazard"],
               "source": _fill(r["source"], th)} for rid, r in th["alert_rules"].items()]
    settings = {k: v for k, v in {**th["derived_fields"], **th["timing"]}.items() if isinstance(v, dict)}
    return render(request, "thresholds.html", groups=groups, alerts=alerts, settings=settings, th=th,
                  version=version, facility=load_facility_config())


def _fill(text: str, th: dict) -> str:
    values = {k: v["value"] for k, v in th["derived_fields"].items() if isinstance(v, dict) and "value" in v}
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return text


@app.get("/e/{eid}/status")
def evaluation_status(eid: str):
    """Polled by the decision page while background work runs, so the page reloads only when it finishes."""
    return {"busy": sorted(service.busy(eid)), "error": service.last_error(eid)}


@app.post("/e/{eid}/ask")
def ask(eid: str, request: Request, question: str = Form(...)):
    if not store.load(eid)["decisions"]:
        return back(f"/e/{eid}", "Questions to the AI open after a decision is recorded")
    if not auth.model_actions.hit(me(request).username):
        return back(f"/e/{eid}#qa", "Too many checks and questions in the last 10 minutes; please wait a few minutes")
    if question.strip():
        service.ask(store, eid, question.strip()[:500], me(request).actor)
    return back(f"/e/{eid}#qa")


@app.post("/e/{eid}/decide")
def decide(eid: str, request: Request, action: str = Form(...), posture: str = Form(""),
           delay_hours: str = Form(""), dismissal_time: str = Form(""), reason: str = Form("")):
    if (denied := managers_only(request, f"/e/{eid}")):
        return denied
    try:
        store.record_decision(eid, action, me(request).actor, reason,
                              posture or None, int(delay_hours) if delay_hours else None, dismissal_time or None)
    except (RecordError, ValueError) as e:
        return back(f"/e/{eid}#decide", str(e))
    return back(f"/e/{eid}")  # top of the page, where the decision banner now shows


@app.post("/e/{eid}/package")
def package(eid: str, request: Request):
    if (denied := managers_only(request, f"/e/{eid}")):
        return denied
    if not auth.model_actions.hit(me(request).username):
        return back(f"/e/{eid}#notify", "Too many AI requests in the last 10 minutes; please wait a few minutes")
    try:
        service.start_package(store, eid)
    except RecordError as e:
        return back(f"/e/{eid}#notify", str(e))
    return back(f"/e/{eid}#notify")


@app.post("/e/{eid}/send")
def send(eid: str, request: Request):
    if (denied := managers_only(request, f"/e/{eid}")):
        return denied
    try:
        store.record_send(eid, me(request).actor)
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
                  erasures=[e for e in store.audit.entries(200) if e["action"] == "record_erased"],
                  tz=state.facility.pin.time_zone)


@app.get("/raw/{fetch_id}")
def raw(fetch_id: str):
    try:
        return JSONResponse(store.raw_body(fetch_id))
    except RecordError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


# ---------- staff and facility ----------

def org_editors_only(request: Request) -> RedirectResponse | None:
    u = me(request)
    return None if (u.is_manager or u.is_sysadmin) else back("/organization", "Only a manager or sysadmin can edit this")


def _form_dict(form) -> dict:
    multi = ("operating_days", "telework_days", "leave_start", "leave_end", "closed_date", "closed_name")
    return {k: (form.getlist(k) if k in multi else form.get(k)) for k in form.keys()}


def _refresh_state():
    state.facility = load_facility_config().repinned(state.facility.pin)  # keep the current location
    state.roster = load_roster_config()


@app.get("/organization", response_class=HTMLResponse)
def organization_page(request: Request):
    u = me(request)
    today = date.today()
    holidays = [(d, n) for y in (today.year, today.year + 1) for d, n in federal_holidays(y).items() if d >= today]
    return render(request, "organization.html", fac=facility_data(), roster=roster_data()["employees"],
                  holidays=holidays,
                  updated=org.updated(), can_edit=u.is_manager or u.is_sysadmin, WEEKDAYS=WEEKDAYS,
                  tz=state.facility.pin.time_zone)


@app.post("/organization/facility")
async def organization_facility(request: Request):
    if (denied := org_editors_only(request)):
        return denied
    try:
        org.save_facility(_form_dict(await request.form()), me(request).actor)
    except OrgError as e:
        return back("/organization#facility", str(e))
    _refresh_state()
    return back("/organization?saved=facility#facility")


@app.get("/organization/employee/{emp_id}", response_class=HTMLResponse)
def employee_page(emp_id: str, request: Request):
    emps = roster_data()["employees"]
    e = next((x for x in emps if x["id"] == emp_id), None)
    if e is None and emp_id != "new":
        return back("/organization#roster", "No such employee")
    u = me(request)
    needs = {a["type"]: a.get("note", "") for a in (e or {}).get("accessibility_needs", [])}
    return render(request, "employee.html", e=e or {}, emp_id=emp_id, needs=needs, ACCESSIBILITY=ACCESSIBILITY,
                  facility_id=facility_data()["id"], WEEKDAYS=WEEKDAYS,
                  is_manager_person=emp_id == facility_data()["manager_employee_id"],
                  can_edit=u.is_manager or u.is_sysadmin)


@app.post("/organization/employee/{emp_id}")
async def employee_save(emp_id: str, request: Request):
    if (denied := org_editors_only(request)):
        return denied
    try:
        new_id = org.save_employee(emp_id, _form_dict(await request.form()), me(request).actor)
    except OrgError as e:
        return back(f"/organization/employee/{emp_id}", str(e))
    _refresh_state()
    return back(f"/organization?saved={new_id}#roster")


@app.post("/organization/employee/{emp_id}/remove")
def employee_remove(emp_id: str, request: Request):
    if (denied := org_editors_only(request)):
        return denied
    try:
        org.remove_employee(emp_id, me(request).actor)
    except OrgError as e:
        return back(f"/organization/employee/{emp_id}", str(e))
    _refresh_state()
    return back("/organization#roster")


@app.get("/healthz")
def healthz():
    return {"ok": True, "db": store.dialect}


@app.on_event("startup")
def start_monitor():
    minutes = float(os.environ.get("MONITOR_MINUTES", "0") or 0)
    if minutes > 0:
        service.run_monitor(store, state, BASE_URL, minutes, os.environ.get("MONITOR_DAY", "next"), pusher)
