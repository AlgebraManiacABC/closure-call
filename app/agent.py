"""Strands agent that investigates and explains a recommendation. It never decides the posture.

It runs only when the rules engine's posture is not "normal". It:
  - reads the free text of the NWS alerts and records the numbers they state, each with the
    sentence quoted word for word; plain code rejects any quote not found verbatim in the alert;
  - does hazard-specific follow-up lookups (upstream river gauge, conditions at commute times,
    latest observation);
  - re-runs the rules engine with altered inputs to test what would change the posture;
  - writes the plain-language rationale paragraph.
`answer_question` answers a manager's follow-up questions from the recorded inputs only.

If the model is unreachable or fails, the report says so and the engine's template summary is
used as the rationale, so the decision page always has something to show.
"""
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta

from pydantic import BaseModel
from strands import Agent, tool

from .llm import make_model
from .rules import Inputs, Recommendation, evaluate, perturbed
from .sources import water
from .sources.http import Fetched, SourceError

HOUR = timedelta(hours=1)


# ---------- report models ----------

class Evidence(BaseModel):
    alert_ref: str  # short id shown to the model, e.g. A1
    alert_id: str
    alert_event: str
    kind: str  # snow_total, ice_total, wind_gust, wind_sustained, river_stage, timing, other
    value: float | None = None
    unit: str
    quote: str  # verified to appear word for word in the alert text
    note: str = ""  # qualifiers, e.g. "applies above 6000 ft; facility is at 4,100 ft"


class Lookup(BaseModel):
    name: str
    result: str
    source: str
    fetched_at: datetime | None = None


class WhatIf(BaseModel):
    change: str
    posture: str
    label: str


class Briefing(BaseModel):
    """What the model returns at the end of an investigation."""
    rationale: str
    caveats: list[str]


class AgentReport(BaseModel):
    ran: bool
    reason: str  # why it did not run, or "ok"
    rationale: str
    caveats: list[str] = []
    evidence: list[Evidence] = []
    rejected_quotes: list[str] = []
    lookups: list[Lookup] = []
    what_ifs: list[WhatIf] = []
    tool_calls: list[str] = []
    model: str = ""
    duration_s: float = 0
    tokens: dict = {}


# ---------- helpers ----------

def _norm(s: str) -> str:
    """Normalize for verbatim matching: unicode form, dashes, quotes, whitespace, case."""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[‐-―−]", "-", s).replace("’", "'").replace("“", '"').replace("”", '"')
    return " ".join(s.split()).lower()


def _fmt_time(t: datetime | None) -> str:
    return t.strftime("%a %H:%M") if t else "further notice"


def _alert_refs(inputs: Inputs) -> dict[str, str]:
    """Short ids (A1, A2, ...) for the hazard alerts, so the model never has to copy long URNs."""
    return {f"A{n}": a.id for n, a in enumerate((a for a in inputs.alerts if a.hazard), start=1)}


WHAT_IF_FIELDS = {
    "gust_mph": ("gust_mph", "add", "mph"),
    "sustained_mph": ("sustained_mph", "add", "mph"),
    "snow_percent": ("snow_in", "scale", "%"),
    "ice_percent": ("ice_in", "scale", "%"),
    "wind_chill_f": ("wind_chill_f", "add", "F"),
    "river_stage_ft": ("gauge_stage_ft", "add", "ft"),
}


def context_text(inputs: Inputs, rec: Recommendation) -> str:
    """Compact briefing of the recorded inputs and the engine's result, kept to a few hundred tokens."""
    tz_now = inputs.evaluated_at
    lines = [
        f"Facility: {inputs.facility_label} (lat {inputs.lat}, lon {inputs.lon}, elevation "
        f"{inputs.elevation_ft:,.0f} ft)" if inputs.elevation_ft else f"Facility: {inputs.facility_label}",
        f"Decision day: {inputs.day:%A %b %d}. Hours {inputs.open:%H:%M}-{inputs.close:%H:%M}, morning commute "
        f"{inputs.morning_commute[0]:%H:%M}-{inputs.morning_commute[1]:%H:%M}, evening commute "
        f"{inputs.evening_commute[0]:%H:%M}-{inputs.evening_commute[1]:%H:%M}. Evaluated {tz_now:%a %H:%M %Z}.",
        "",
        f"RULES ENGINE RECOMMENDATION (final; do not change it): {rec.posture_label}.",
        f"Timing: {rec.timing_explanation}"
        + (f" The decision must be announced by {rec.announce_by:%a %H:%M}." if rec.announce_by else ""),
        f"Confidence: {rec.confidence}. " + " ".join(rec.confidence_reasons),
        "Decisive findings:",
    ]
    for f in rec.decisive:
        span = f"{_fmt_time(f.first_hour)}-{(f.last_hour + HOUR):%H:%M}"
        if f.kind == "field":
            lines.append(f"  - {f.rule_id}: {f.value} {f.unit} vs threshold {f.threshold} "
                         f"(margin {f.margin:+}), {span}, from {inputs.sources[f.source_key].label}")
        else:
            lines.append(f"  - {f.rule_id}: NWS {f.alert_event} ({f.alert_certainty}) in effect {span}")
    if not rec.decisive:
        lines.append("  - none")
    near = [c for c in rec.checks if c.peak is not None and (c.crossed or c.margin > -0.25 * abs(c.threshold or 1))]
    if near:
        lines.append("Forecast values at the facility vs thresholds (peak during commute/work hours):")
        lines += [f"  - {c.rule_id}: peak {c.peak} {c.unit} vs {c.threshold} "
                  f"({'crossed' if c.crossed else 'short'} by {abs(c.margin)})" for c in near]
    refs = {v: k for k, v in _alert_refs(inputs).items()}
    if refs:
        lines.append("NWS alerts at the facility location (read the text with read_alert):")
        for a in inputs.alerts:
            if a.id in refs:
                lines.append(f"  - {refs[a.id]}: {a.event} ({a.severity}/{a.certainty}), in effect "
                             f"{_fmt_time(a.starts)} to {_fmt_time(a.ends)}, issued {a.sent} by {a.sender}")
    if inputs.gauge:
        g = inputs.gauge
        lines.append(f"River gauge {g.lid} {g.name}, {g.distance_mi} mi away: now {g.latest_stage_ft} ft, "
                     f"forecast crest {g.crest_stage_ft} ft at {g.crest_time}; NWS flood stages {g.stages_ft}.")
        if rec.flood_impact:
            lines.append(f"  NWS impact statement at {rec.flood_impact.stage_ft} ft: {rec.flood_impact.statement}")
    if rec.what_would_change:
        lines.append("Already computed by the engine, what would change the posture:")
        lines += [f"  - {c.what} -> {c.detail}" for c in rec.what_would_change]
    lines.append("Lookups available: " + ", ".join(available_lookups(inputs)))
    return "\n".join(lines)


def available_lookups(inputs: Inputs) -> list[str]:
    names = ["commute_hours", "observation"]
    if inputs.gauge:
        names.append("upstream_river")
    return names


SYSTEM_PROMPT = """You support a federal facility manager deciding whether to close for severe weather.
A deterministic rules engine has ALREADY chosen the posture. You do not change it or second-guess it.
Your job is to investigate and explain it, using only the recorded inputs and your tools.

Rules:
- Every number you take from alert text must first be recorded with record_evidence, quoting the
  sentence word for word. If a quote is rejected, copy it again exactly or drop it.
- Note qualifiers that limit whether a statement applies to this facility (elevation, county,
  "above X feet", "in the mountains"), comparing them with the facility's elevation and location.
- Do not invent thresholds, numbers, or reasons. Explain the posture using the engine's decisive
  findings and timing as given.
- Never assume the facility lies inside a named valley, county or zone unless the record says so.
  If an alert names places, say whether the facility's town is among them; if unclear, say it is unclear.
- Keep tool use short: read each alert once, at most 2 lookups, at most 3 what_if calls.

The final rationale is ONE paragraph of at most 120 words a manager could paste into an email:
state the posture, the main reason with its source (NWS alert, forecast or river gauge), the timing,
and how confident the recommendation is. Plain language: no rule ids, no alert ids like A1 (name the
alert, e.g. "the NWS Winter Storm Warning"). Caveats: at most 3 short items."""


# ---------- the investigation ----------

class Investigation:
    def __init__(self, inputs: Inputs, rec: Recommendation):
        self.inputs, self.rec = inputs, rec
        self.refs = _alert_refs(inputs)
        self.alerts = {a.id: a for a in inputs.alerts}
        self.evidence: list[Evidence] = []
        self.rejected: list[str] = []
        self.lookups: list[Lookup] = []
        self.what_ifs: list[WhatIf] = []
        self.calls: list[str] = []
        self.fetched: list[Fetched] = []  # raw responses from network lookups, for the decision record

    def _alert(self, ref: str):
        return self.alerts.get(self.refs.get(ref.strip().upper(), ref.strip()))

    def tools(self, network: bool = True) -> list:
        inv = self

        @tool
        def read_alert(alert_id: str) -> str:
            """Return the full text of one NWS alert at the facility location.

            Args:
                alert_id: The short alert id from the briefing, for example A1.
            """
            inv.calls.append(f"read_alert({alert_id})")
            a = inv._alert(alert_id)
            if not a:
                return f"No alert {alert_id}. Valid ids: {', '.join(inv.refs)}"
            text = f"{a.event} from {a.sender}. Area: {a.area}\n{a.description}\n{a.instruction}"
            return text[:3000]

        @tool
        def record_evidence(alert_id: str, quote: str, kind: str, value: float | None = None, unit: str = "",
                            note: str = "") -> str:
            """Record one fact stated in an alert, with the exact sentence it came from.

            Args:
                alert_id: The short alert id, for example A1.
                quote: The sentence copied word for word from the alert text.
                kind: One of snow_total, ice_total, wind_gust, wind_sustained, river_stage, timing, other.
                value: The number; for a range use the upper end. Omit if the sentence has no number.
                unit: The unit, for example in, mph, ft, hour.
                note: Qualifiers that limit where it applies, compared with the facility. Optional.
            """
            inv.calls.append(f"record_evidence({alert_id}, {kind}, {value} {unit})")
            a = inv._alert(alert_id)
            if not a:
                return f"REJECTED: no alert {alert_id}."
            haystack = _norm(f"{a.description}\n{a.instruction}\n{a.headline}")
            if not quote.strip() or _norm(quote) not in haystack:
                inv.rejected.append(quote)
                return "REJECTED: that quote is not word for word in the alert. Copy the sentence exactly."
            inv.evidence.append(Evidence(alert_ref=alert_id.strip().upper(), alert_id=a.id, alert_event=a.event,
                                         kind=kind, value=value, unit=unit, quote=quote.strip(), note=note))
            return "recorded"

        @tool
        def what_if(change: str, amount: float = 0, alert_id: str = "") -> str:
            """Re-run the rules engine with one input changed and return the resulting posture.

            Args:
                change: One of gust_mph, sustained_mph, wind_chill_f, river_stage_ft (amount is added),
                    snow_percent, ice_percent (amount is a percent change, e.g. 50 or -50),
                    remove_alert or upgrade_watch (give alert_id).
                amount: The amount to add or the percent change.
                alert_id: The short alert id, for remove_alert or upgrade_watch.
            """
            inv.calls.append(f"what_if({change}, {amount}, {alert_id})")
            return inv.what_if(change, amount, alert_id)

        @tool
        def lookup(name: str) -> str:
            """Run a follow-up lookup. Names: commute_hours (hourly temperature, wind, snow, ice and
            hazard level during the commutes), observation (latest observed conditions vs forecast),
            upstream_river (current level and crest forecast at the next gauge upstream).

            Args:
                name: commute_hours, observation, or upstream_river.
            """
            inv.calls.append(f"lookup({name})")
            return inv.lookup(name, network)

        return [read_alert, record_evidence, what_if, lookup]

    def what_if(self, change: str, amount: float, alert_id: str) -> str:
        inputs = self.inputs
        if change in WHAT_IF_FIELDS:
            field, mode, unit = WHAT_IF_FIELDS[change]
            trial = perturbed(inputs, field, 1 + amount / 100 if mode == "scale" else amount, mode)
            desc = f"{change.replace('_', ' ')} {amount:+g}{'%' if mode == 'scale' else ' ' + unit}"
        elif change in ("remove_alert", "upgrade_watch"):
            a = self._alert(alert_id)
            if not a:
                return f"No alert {alert_id}."
            if change == "remove_alert":
                alerts = [x for x in inputs.alerts if x.id != a.id]
                desc = f"{a.event} cancelled"
            else:
                up = a.event.replace("Watch", "Warning")
                alerts = [x.model_copy(update={"event": up, "certainty": "Likely"}) if x.id == a.id else x
                          for x in inputs.alerts]
                desc = f"{a.event} upgraded to {up}"
            trial = inputs.model_copy(update={"alerts": alerts})
        else:
            return f"Unknown change '{change}'."
        r = evaluate(trial, explore=False)
        self.what_ifs.append(WhatIf(change=desc, posture=r.posture, label=r.posture_label))
        same = " (unchanged)" if r.posture == self.rec.posture else ""
        return f"{desc}: {r.posture_label}{same}. {r.timing_explanation}"

    def lookup(self, name: str, network: bool) -> str:
        inputs, rec = self.inputs, self.rec
        if name == "commute_hours":
            levels = {h["start"]: h["level"] for h in rec.hourly_levels}
            rows = []
            for h in inputs.hours:
                t = h.start.time()
                if h.start.date() == inputs.day and (
                        inputs.morning_commute[0].hour <= t.hour <= inputs.morning_commute[1].hour or
                        inputs.evening_commute[0].hour <= t.hour < inputs.evening_commute[1].hour):
                    def n(v, unit):
                        return "n/a" if v is None else f"{round(v)}{unit}"
                    rows.append(f"{h.start:%H:%M} temp {n(h.temperature_f, 'F')} gust {n(h.gust_mph, 'mph')} "
                                f"snow {h.snow_in or 0:.2f}in ice {h.ice_in or 0:.2f}in "
                                f"windchill {n(h.wind_chill_f, 'F')} level {levels.get(h.start, '-')}")
            result, src = "\n".join(rows) or "no hourly data", inputs.sources["grid"]
        elif name == "observation":
            o = inputs.observation
            if not o:
                return "No recent observation recorded."
            result = (f"{o['station_name']} at {o['timestamp']}: {o['text']}, temp "
                      f"{o['temperature_f'] and round(o['temperature_f'])}F, wind "
                      f"{o['wind_speed_mph'] and round(o['wind_speed_mph'])} mph, gust "
                      f"{o['wind_gust_mph'] and round(o['wind_gust_mph'])} mph")
            src = inputs.sources["obs"]
        elif name == "upstream_river":
            if not inputs.gauge:
                return "No river gauge is tracked for this facility."
            if not network:
                return "Upstream lookup was not part of the recorded investigation."
            try:
                detail, f1 = water.gauge_detail(inputs.gauge.lid)
                if not detail.upstream_lid:
                    return f"NWS lists no upstream gauge for {inputs.gauge.lid}."
                up, f2 = water.gauge_detail(detail.upstream_lid)
                sf, f3 = water.stageflow(detail.upstream_lid)
            except SourceError as e:
                return f"Lookup failed: {e}"
            self.fetched += [f1, f2, f3]
            parts = [f"Upstream gauge {up.lid} {up.name}"]
            parts.append(f"latest {sf.latest.stage_ft} ft at {sf.latest.time}" if sf.latest
                         else "no current readings published")
            if sf.crest:
                trend = "rising" if sf.forecast[-1].stage_ft > sf.forecast[0].stage_ft else "falling or steady"
                parts.append(f"forecast crest {sf.crest.stage_ft} ft at {sf.crest.time} ({trend})")
            else:
                parts.append("no river forecast published")
            parts.append(f"NWS flood stages {up.flood_stages_ft}" if up.flood_stages_ft else "no NWS flood stages defined")
            result = "; ".join(parts) + "."
            self.lookups.append(Lookup(name=name, result=result, source=f"NOAA NWPS gauge {up.lid}",
                                       fetched_at=f3.fetched_at))
            return result
        else:
            return f"Unknown lookup '{name}'. Available: {', '.join(available_lookups(inputs))}"
        self.lookups.append(Lookup(name=name, result=result, source=src.label, fetched_at=src.fetched_at))
        return result

    def report(self, ran: bool, reason: str, rationale: str, caveats: list[str], started: float,
               model: str = "", tokens: dict | None = None) -> AgentReport:
        return AgentReport(ran=ran, reason=reason, rationale=rationale, caveats=caveats, evidence=self.evidence,
                           rejected_quotes=self.rejected, lookups=self.lookups, what_ifs=self.what_ifs,
                           tool_calls=self.calls, model=model, duration_s=round(time.time() - started, 1),
                           tokens=tokens or {})


def investigate(inputs: Inputs, rec: Recommendation, force: bool = False) -> tuple[AgentReport, list[Fetched]]:
    started = time.time()
    inv = Investigation(inputs, rec)
    if rec.posture == "normal" and not force:
        return inv.report(False, "Posture is normal; agent not needed.", rec.summary, [], started), []

    task = (context_text(inputs, rec) + "\n\nTask:\n"
            "1. Read each alert listed above and record the numbers it states that bear on this facility "
            "(snow, ice, wind, river stage, start and end times), noting any qualifiers.\n"
            "2. Run the lookups that matter for this hazard.\n"
            "3. Use what_if for up to 3 scenarios a manager would ask about.\n"
            "4. Finish with the rationale paragraph and caveats.")
    try:
        agent = Agent(model=make_model(max_tokens=1500), tools=inv.tools(), system_prompt=SYSTEM_PROMPT,
                      callback_handler=None)
        result = agent(task, structured_output_model=Briefing, limits={"turns": 14})
        b: Briefing | None = result.structured_output
        if b is None:
            raise RuntimeError(f"no structured output (stop reason {result.stop_reason})")
        usage = dict(result.metrics.accumulated_usage)
        return inv.report(True, "ok", b.rationale.strip(), b.caveats[:3], started,
                          model=os.environ.get("CHAT_MODEL", ""), tokens=usage), inv.fetched
    except Exception as e:  # the decision page must still work without the model
        return inv.report(False, f"Agent failed: {type(e).__name__}: {e}"[:300], rec.summary, [], started), inv.fetched


QA_PROMPT = """You answer a facility manager's questions about a closure recommendation.
Answer ONLY from the recorded briefing below and your tools, which read the same recorded data.
Name the source and time for any figure you give (for example "NWS forecast issued 14:09").
If the record does not contain the answer, say so plainly and suggest what to check.
Never assume the facility lies inside a named valley, county or zone unless the record says so.
Never change the recommended posture. Answer in at most 120 words."""


def answer_question(inputs: Inputs, rec: Recommendation, report: AgentReport | None, question: str) -> str:
    """Answers from recorded inputs only: no network lookups, so the answer matches the decision record."""
    inv = Investigation(inputs, rec)
    ctx = context_text(inputs, rec)
    if report and report.ran:
        ctx += "\n\nAgent findings already recorded:\n" + "\n".join(
            [f"- {e.alert_event}: \"{e.quote}\" ({e.kind} {e.value} {e.unit}){' - ' + e.note if e.note else ''}"
             for e in report.evidence] + [f"- lookup {l.name}: {l.result}" for l in report.lookups])
        ctx += f"\nRationale given to the manager: {report.rationale}"
    try:
        agent = Agent(model=make_model(max_tokens=800), tools=inv.tools(network=False),
                      system_prompt=QA_PROMPT + "\n\n" + ctx, callback_handler=None)
        return str(agent(question, limits={"turns": 6})).strip()
    except Exception as e:
        return f"The assistant is unavailable right now ({type(e).__name__}). The recorded inputs are shown on the page."
