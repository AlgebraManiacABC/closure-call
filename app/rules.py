"""Deterministic rules engine: picks a closure posture from structured inputs.

Pure code. No network, no clock, no model: `evaluate(inputs)` reads only its arguments, so
identical inputs always give the identical recommendation. Every cutoff comes from
data/thresholds.json, where each sits next to its source.

How it decides:
  1. Each hour of the decision day gets a level per hazard from field rules (forecast values vs
     thresholds) and alert rules (NWS alerts in effect): 0 none, 1 elevated, 2 hazardous.
  2. A timing step turns the hazardous hours into a posture: a delayed arrival if they clear
     soon after the morning commute, an early dismissal if they arrive mid-day, a closure if
     they persist or would force staff home before noon. Elevated hours alone give the option
     of unscheduled leave or telework.
  3. Confidence and "what would change" are computed from the same inputs; what-would-change
     re-runs the engine on perturbed copies of the inputs.
"""
import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

THRESHOLDS_PATH = Path(__file__).resolve().parent.parent / "data" / "thresholds.json"
HOUR = timedelta(hours=1)
POSTURE_RANK = {"normal": 0, "unscheduled_leave": 1, "early_dismissal": 2, "delayed_arrival": 2, "closure": 3}
FIELD_SOURCE = {"gust_mph": "grid", "sustained_mph": "grid", "snow_in": "grid", "ice_in": "grid",
                "wind_chill_f": "grid", "snow_storm_in": "grid", "ice_storm_in": "grid", "gauge_stage_ft": "gauge",
                "gauge_stage_held_ft": "gauge"}


# ---------- inputs ----------

class SourceRef(BaseModel):
    source: str  # nws / nwps / usgs
    label: str  # human-readable, e.g. "NWS gridpoint forecast DVN/63,81"
    url: str
    fetched_at: datetime
    issued: str | None = None  # when the provider issued or updated the data


class AlertIn(BaseModel):
    id: str
    event: str
    hazard: str | None
    severity: str
    certainty: str
    urgency: str
    starts: datetime
    ends: datetime | None  # None = until further notice
    headline: str
    sender: str
    sent: str = ""  # when NWS issued this version of the alert
    area: str = ""
    description: str = ""  # free text; the agent quotes from it, the engine never reads it
    instruction: str = ""

    def active(self, start: datetime, end: datetime) -> bool:
        return self.starts < end and (self.ends is None or self.ends > start)


class HourIn(BaseModel):
    start: datetime  # local, timezone-aware
    gust_mph: float | None = None
    sustained_mph: float | None = None
    snow_in: float | None = None  # accumulation within this hour
    ice_in: float | None = None
    wind_chill_f: float | None = None
    gauge_stage_ft: float | None = None
    gauge_extrapolated: bool = False  # beyond the end of the river forecast
    temperature_f: float | None = None  # context for the agent (rain vs snow, refreeze); not used by rules


class Impact(BaseModel):
    stage_ft: float
    statement: str


class GaugeIn(BaseModel):
    lid: str
    name: str
    distance_mi: float
    stages_ft: dict[str, float]
    impacts: list[Impact]  # highest stage first
    latest_stage_ft: float | None
    latest_time: str | None
    crest_stage_ft: float | None
    crest_time: str | None
    forecast_issued: str | None
    usgs_site: str | None = None
    usgs_stage_ft: float | None = None
    usgs_time: str | None = None


class Inputs(BaseModel):
    facility_id: str
    facility_label: str
    lat: float
    lon: float
    elevation_ft: float | None = None  # gridpoint elevation; lets the agent judge "above 6000 feet" wording
    county: str | None = None  # e.g. "Clinton County, IA"; context for the agent, not used by rules
    time_zone: str
    day: date
    evaluated_at: datetime  # the "now" the evaluation is for; part of the inputs so re-runs match
    open: time
    close: time
    morning_commute: tuple[time, time]
    evening_commute: tuple[time, time]
    decide_by: time  # deadline to announce a delay or closure
    hours: list[HourIn]  # hourly, from noon the day before to the evening of `day`
    alerts: list[AlertIn]
    gauge: GaugeIn | None = None
    observation: dict | None = None
    sources: dict[str, SourceRef]  # keys: point, alerts, grid, gauge, usgs, obs
    notes: list[str] = []

    def at(self, t: time) -> datetime:
        return datetime.combine(self.day, t, tzinfo=ZoneInfo(self.time_zone))


# ---------- outputs ----------

class Finding(BaseModel):
    """One rule crossed in one or more hours."""
    rule_id: str
    hazard: str
    level: int
    kind: str  # field / alert
    field: str | None = None
    value: float | None = None  # worst value over the hours it fired
    threshold: float | None = None
    unit: str | None = None
    margin: float | None = None  # how far past the threshold, in the threshold's unit
    first_hour: datetime
    last_hour: datetime
    alert_id: str | None = None
    alert_event: str | None = None
    alert_certainty: str | None = None
    source_key: str  # which entry in Inputs.sources the value came from
    threshold_source: str


class Check(BaseModel):
    """A field threshold checked against the worst value in the decision window, crossed or not."""
    rule_id: str
    hazard: str
    field: str
    level: int
    threshold: float
    unit: str
    peak: float | None
    peak_hour: datetime | None
    margin: float | None  # positive = crossed by this much; negative = short by this much
    crossed: bool
    threshold_source: str


class Change(BaseModel):
    what: str
    posture: str
    detail: str


class Recommendation(BaseModel):
    facility_id: str
    facility_label: str
    day: date
    evaluated_at: datetime
    rules_version: str
    posture: str
    posture_label: str
    delay_hours: int | None = None
    dismissal_time: datetime | None = None
    unscheduled_leave_option: bool
    announce_by: datetime | None
    timing_rule: str | None
    timing_explanation: str
    decisive: list[Finding]
    findings: list[Finding]
    checks: list[Check]
    alerts_considered: list[AlertIn]
    watches: list[AlertIn]
    hazard_peak_levels: dict[str, int]
    hourly_levels: list[dict]
    flood_impact: Impact | None
    confidence: str
    confidence_reasons: list[str]
    what_would_change: list[Change]
    summary: str


# ---------- thresholds ----------

def thresholds_text(path: Path = THRESHOLDS_PATH) -> str:
    """The rules file with line endings normalized, so a version hash means the same rules on any OS."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def rules_version(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def load_thresholds(path: Path = THRESHOLDS_PATH) -> tuple[dict, str]:
    text = thresholds_text(path)
    return json.loads(text), rules_version(text)


def _resolve(value, inputs: Inputs) -> float | None:
    """Thresholds are numbers, or 'gauge:<category>' for a gauge's own NWS flood stage."""
    if isinstance(value, str) and value.startswith("gauge:"):
        g = inputs.gauge
        return g.stages_ft.get(value.split(":", 1)[1]) if g else None
    return float(value)


def _crosses(op: str, v: float, thr: float) -> bool:
    return v >= thr if op == ">=" else v <= thr


def _margin(op: str, v: float, thr: float) -> float:
    return round(v - thr if op == ">=" else thr - v, 2)


# ---------- derived hourly fields ----------

class _Keep(dict):
    def __missing__(self, key):  # leave unknown {names} as written
        return "{" + key + "}"


def _source(rule: dict, th: dict) -> str:
    """A rule's source text with {setting} placeholders filled from the same thresholds file,
    so explanations state the value ("3 hours"), not the setting's name."""
    values = {k: v["value"] for k, v in th.get("derived_fields", {}).items() if isinstance(v, dict) and "value" in v}
    return rule["source"].format_map(_Keep(values))


def _storm_totals(hours: list[HourIn], field: str, recovery_h: int) -> list[float | None]:
    """12-hour accumulation as of the last hour with any, counted while falling or within
    `recovery_h` hours after it stops; 0 otherwise. None where there is no forecast data."""
    vals = [getattr(h, field) for h in hours]
    out: list[float | None] = []
    last = None
    for i, v in enumerate(vals):
        if v is None:
            out.append(None)
            continue
        if v > 0.005:
            last = i
        if last is not None and i - last <= recovery_h:
            out.append(round(sum(x or 0 for x in vals[max(0, last - 11): last + 1]), 2))
        else:
            out.append(0.0)
    return out


def _rows(inputs: Inputs, th: dict) -> list[dict]:
    recovery = th["derived_fields"]["road_recovery_hours"]["value"]
    snow = _storm_totals(inputs.hours, "snow_in", recovery)
    ice = _storm_totals(inputs.hours, "ice_in", recovery)
    hold = th["derived_fields"]["flood_recession_hours"]["value"]
    stages = [h.gauge_stage_ft for h in inputs.hours]
    held = [max((s for s in stages[max(0, i - hold): i + 1] if s is not None), default=None) for i in range(len(stages))]
    return [{**h.model_dump(), "snow_storm_in": s, "ice_storm_in": i, "gauge_stage_held_ft": g}
            for h, s, i, g in zip(inputs.hours, snow, ice, held)]


# ---------- evaluation ----------

def _window(inputs: Inputs) -> tuple[datetime, datetime]:
    """Hours that matter for the decision: start of the morning commute to end of the evening commute."""
    start = inputs.at(inputs.morning_commute[0]).replace(minute=0)
    end_t = inputs.at(inputs.evening_commute[1])
    end = end_t if end_t.minute == 0 else end_t.replace(minute=0) + HOUR
    return start, end


def _merge(findings: list[Finding]) -> list[Finding]:
    """Collapse per-hour findings into one per rule (and alert), keeping the worst value."""
    merged: dict[tuple, Finding] = {}
    for f in findings:
        key = (f.rule_id, f.alert_id)
        m = merged.get(key)
        if m is None:
            merged[key] = f.model_copy()
            continue
        m.first_hour, m.last_hour = min(m.first_hour, f.first_hour), max(m.last_hour, f.last_hour)
        if f.margin is not None and (m.margin is None or f.margin > m.margin):
            m.value, m.margin = f.value, f.margin
    return sorted(merged.values(), key=lambda f: (-f.level, f.first_hour, f.rule_id))


def _round_down_half_hour(t: datetime) -> datetime:
    return t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0)


def _round_up_half_hour(t: datetime) -> datetime:
    r = _round_down_half_hour(t)
    return r if r == t else r + timedelta(minutes=30)


def evaluate(inputs: Inputs, thresholds: dict | None = None, rules_version: str | None = None,
             explore: bool = True) -> Recommendation:
    if thresholds is None:
        thresholds, rules_version = load_thresholds()
    th = thresholds
    rows = _rows(inputs, th)
    w_start, w_end = _window(inputs)
    now = inputs.evaluated_at

    in_window = [i for i, r in enumerate(rows) if w_start <= r["start"] < w_end and r["start"] + HOUR > now]

    # 1. Per-hour findings from field rules and alert rules.
    per_hour: dict[int, list[Finding]] = {i: [] for i in in_window}
    for rid, rule in th["field_rules"].items():
        thr = _resolve(rule["value"], inputs)
        if thr is None:
            continue
        for i in in_window:
            v = rows[i][rule["field"]]
            if v is not None and _crosses(rule["op"], v, thr):
                per_hour[i].append(Finding(
                    rule_id=rid, hazard=rule["hazard"], level=rule["level"], kind="field", field=rule["field"],
                    value=round(v, 2), threshold=thr, unit=rule["unit"], margin=_margin(rule["op"], v, thr),
                    first_hour=rows[i]["start"], last_hour=rows[i]["start"],
                    source_key=FIELD_SOURCE[rule["field"]], threshold_source=_source(rule, th)))

    alerts = [a for a in inputs.alerts if a.hazard and a.certainty != "Unlikely"]
    recovery = timedelta(hours=th["derived_fields"]["road_recovery_hours"]["value"])
    for rid, rule in th["alert_rules"].items():
        for a in alerts:
            if a.event not in rule["events"]:
                continue
            if rule.get("extend_by_road_recovery") and a.ends:
                a = a.model_copy(update={"ends": a.ends + recovery})
            for i in in_window:
                if a.active(rows[i]["start"], rows[i]["start"] + HOUR):
                    per_hour[i].append(Finding(
                        rule_id=rid, hazard=rule["hazard"], level=rule["level"], kind="alert",
                        first_hour=rows[i]["start"], last_hour=rows[i]["start"], alert_id=a.id,
                        alert_event=a.event, alert_certainty=a.certainty, source_key="alerts",
                        threshold_source=_source(rule, th)))

    level = {i: max((f.level for f in per_hour[i]), default=0) for i in in_window}
    hazardous = [i for i in in_window if level[i] >= 2]
    elevated = any(level[i] >= 1 for i in in_window)

    # 2. Timing: hazardous hours -> posture.
    t = th["timing"]
    morning_start, morning_end = inputs.at(inputs.morning_commute[0]), inputs.at(inputs.morning_commute[1])
    open_dt, close_dt = inputs.at(inputs.open), inputs.at(inputs.close)
    before_open = now < open_dt
    posture, delay, dismissal, timing_rule = "normal", None, None, None

    if not hazardous:
        posture = "unscheduled_leave" if elevated else "normal"
        timing_expl = ("Advisory-level conditions during working or commuting hours, but nothing at warning level."
                       if elevated else "No threshold crossed during commuting or working hours.")
    else:
        first = rows[hazardous[0]]["start"]
        last_end = rows[hazardous[-1]]["start"] + HOUR
        if first < morning_end and now < morning_start:
            for n in t["delay_options_hours"]["value"]:
                if last_end <= morning_start + timedelta(hours=n):
                    posture, delay, timing_rule = "delayed_arrival", n, "TIME-DELAY"
                    timing_expl = (f"Hazardous conditions affect the morning commute and end by {last_end:%H:%M}; "
                                   f"a {n}-hour delay moves the commute past them.")
                    break
            else:
                posture, timing_rule = "closure", "TIME-CLOSE-PERSISTS"
                timing_expl = (f"Hazardous conditions affect the morning commute and last until {last_end:%H:%M}, "
                               f"beyond the longest delay considered ({max(t['delay_options_hours']['value'])} hours).")
        else:
            lead = timedelta(hours=t["early_dismissal_lead_hours"]["value"])
            dismiss = _round_down_half_hour(first - lead)
            noon = inputs.at(time.fromisoformat(t["closure_if_dismissal_before"]["value"]))
            if before_open and dismiss < noon:
                posture, timing_rule = "closure", "TIME-CLOSE-EARLY-HAZARD"
                timing_expl = (f"Hazardous conditions begin at {first:%H:%M}; dismissing {lead.seconds // 3600} hours "
                               f"ahead would mean leaving at {dismiss:%H:%M}, before {noon:%H:%M}, so staff should not commute in.")
            elif dismiss >= close_dt:
                posture = "unscheduled_leave"
                timing_expl = f"Hazardous conditions begin at {first:%H:%M}, after staff are expected home."
            else:
                dismissal = max(dismiss, _round_up_half_hour(now))
                posture, timing_rule = "early_dismissal", "TIME-EARLY-DISMISS"
                timing_expl = (f"Hazardous conditions begin at {first:%H:%M}; dismissing at {dismissal:%H:%M} "
                               f"gets staff home before them.")

    leave_option = elevated and posture != "closure"
    labels = th["postures"]
    label = labels[posture].format(hours=delay, time=dismissal.strftime("%H:%M") if dismissal else "")
    if leave_option and posture in ("delayed_arrival", "early_dismissal"):
        label += ", with option for unscheduled leave or unscheduled telework"

    if posture in ("closure", "delayed_arrival"):
        announce_by = inputs.at(inputs.decide_by)
    elif posture == "early_dismissal":
        announce_by = dismissal - HOUR
    else:
        announce_by = None

    # 3. Findings, decisive findings, threshold checks.
    all_findings = _merge([f for i in in_window for f in per_hour[i]])
    if hazardous:
        decisive = _merge([f for i in hazardous for f in per_hour[i] if f.level >= 2])
    else:
        decisive = [f for f in all_findings if f.level >= 1]

    checks = []
    for rid, rule in th["field_rules"].items():
        thr = _resolve(rule["value"], inputs)
        if thr is None:
            continue
        vals = [(rows[i][rule["field"]], rows[i]["start"]) for i in in_window if rows[i][rule["field"]] is not None]
        if vals:
            peak, peak_hour = (max if rule["op"] == ">=" else min)(vals, key=lambda x: x[0])
            m = _margin(rule["op"], peak, thr)
        else:
            peak = peak_hour = m = None
        checks.append(Check(rule_id=rid, hazard=rule["hazard"], field=rule["field"], level=rule["level"],
                            threshold=thr, unit=rule["unit"], peak=None if peak is None else round(peak, 2),
                            peak_hour=peak_hour, margin=m, crossed=m is not None and m >= 0,
                            threshold_source=_source(rule, th)))

    hazards = ("flood", "wind", "winter")
    hourly = [{"start": rows[i]["start"], "level": level[i],
               "hazards": sorted({f.hazard for f in per_hour[i] if f.level == level[i] and level[i] > 0})}
              for i in in_window]
    peak_levels = {h: max((f.level for f in all_findings if f.hazard == h), default=0) for h in hazards}

    watch_events = set(th["watch_events"]["events"])
    watches = [a for a in inputs.alerts if a.event in watch_events and a.active(w_start, w_end)]
    considered = [a for a in alerts if a.active(w_start, w_end) and a.event not in watch_events]

    flood_impact = None
    if inputs.gauge and inputs.gauge.impacts:
        stages = [rows[i]["gauge_stage_ft"] for i in in_window if rows[i]["gauge_stage_ft"] is not None]
        if stages:
            top = max(stages)
            flood_impact = next((imp for imp in inputs.gauge.impacts if imp.stage_ft <= top), None)

    # 4. Confidence.
    conf = th["confidence"]
    reasons = []
    downgrades = 0
    lead_h = ((rows[hazardous[0]]["start"] if hazardous else w_start) - now).total_seconds() / 3600
    if lead_h > conf["lead_time_low_hours"]:
        downgrades += 2
        reasons.append(f"Decision is {lead_h:.0f} hours ahead; forecasts this far out change often.")
    elif lead_h > conf["lead_time_medium_hours"]:
        downgrades += 1
        reasons.append(f"Decision is {lead_h:.0f} hours ahead; the forecast may still shift.")
    top_level = max(level.values(), default=0)
    for c in checks:
        near = conf["borderline_margin"].get(c.field)
        if c.margin is None or near is None or abs(c.margin) > near:
            continue
        if c.field in SCALED and c.peak <= 0:  # no snow or ice at all is not "nearly" an advisory
            continue
        decides = any(f.rule_id == c.rule_id for f in decisive)
        if decides or (not c.crossed and c.level > top_level):
            downgrades += 1
            reasons.append(f"{c.rule_id} is borderline: peak {c.peak} {c.unit} against {c.threshold} "
                           f"({'crossed' if c.crossed else 'short'} by {abs(c.margin)}).")
            break
    dec_alert = [f for f in decisive if f.kind == "alert"]
    if dec_alert and not [f for f in decisive if f.kind == "field"]:
        if all(f.alert_certainty in ("Possible", "Unknown") for f in dec_alert):
            downgrades += 1
            reasons.append("The deciding NWS alert rates the event only 'Possible'.")
        downgrades += 1
        reasons.append("The NWS alert is at a higher level than the forecast values at this exact location; "
                       "the alert may cover a wider or higher area (check its text).")
    if any(f.field == "gauge_stage_held_ft" for f in decisive) and \
            any(rows[i]["gauge_extrapolated"] for i in (hazardous or in_window)):
        downgrades += 1
        reasons.append("River levels for part of the day are beyond the end of the NWS river forecast.")
    missing = sum(1 for i in in_window if rows[i]["gust_mph"] is None)
    if in_window and missing > len(in_window) / 2:
        downgrades += 1
        reasons.append("Forecast grid values are missing for much of the day.")
    if watches and posture != "closure":
        downgrades += 1
        reasons.append("A watch is in effect: " + ", ".join(sorted({a.event for a in watches})) + ".")
    confidence = ["high", "medium", "low"][min(downgrades, 2)]
    if not reasons:
        reasons.append("Observed or likely conditions, well clear of thresholds, with short lead time.")

    summary = _summary(inputs, posture, label, decisive, timing_expl, confidence, flood_impact)

    rec = Recommendation(
        facility_id=inputs.facility_id, facility_label=inputs.facility_label, day=inputs.day,
        evaluated_at=now, rules_version=rules_version or "", posture=posture, posture_label=label,
        delay_hours=delay, dismissal_time=dismissal, unscheduled_leave_option=leave_option,
        announce_by=announce_by, timing_rule=timing_rule, timing_explanation=timing_expl,
        decisive=decisive, findings=all_findings, checks=checks, alerts_considered=considered,
        watches=watches, hazard_peak_levels=peak_levels, hourly_levels=hourly, flood_impact=flood_impact,
        confidence=confidence, confidence_reasons=reasons, what_would_change=[], summary=summary)
    if explore:
        rec.what_would_change = what_would_change(inputs, rec, th, rules_version)
    return rec


def _summary(inputs: Inputs, posture: str, label: str, decisive: list[Finding], timing: str,
             confidence: str, impact: Impact | None) -> str:
    """Template fallback for the rationale; the agent writes the real paragraph."""
    s = f"{label} for {inputs.facility_label} on {inputs.day:%A %b %d}. "
    if decisive:
        f = decisive[0]
        if f.kind == "field":
            s += (f"Deciding factor: {f.rule_id}, {f.value} {f.unit} against a threshold of {f.threshold} "
                  f"({'+' if f.margin >= 0 else ''}{f.margin}), from {f.first_hour:%H:%M} to {f.last_hour + HOUR:%H:%M}. ")
        else:
            s += f"Deciding factor: NWS {f.alert_event} in effect from {f.first_hour:%H:%M} to {f.last_hour + HOUR:%H:%M}. "
    s += timing + " "
    if impact:
        s += f"NWS impact statement at {impact.stage_ft} ft: \"{impact.statement}\" "
    return s + f"Confidence: {confidence}."


# ---------- what would change ----------

FIELD_NAMES = {"gust_mph": "Wind gusts", "sustained_mph": "Sustained wind", "snow_storm_in": "Snowfall",
               "ice_storm_in": "Ice accumulation", "wind_chill_f": "Wind chill", "gauge_stage_held_ft": "River stage"}
SCALED = {"snow_storm_in": "snow_in", "ice_storm_in": "ice_in"}  # derived totals change via their raw field
SHIFTED = {"gauge_stage_held_ft": "gauge_stage_ft"}  # derived values shifted via their raw field


def perturbed(inputs: Inputs, field: str, amount: float, mode: str) -> Inputs:
    """Copy of inputs with one hourly field shifted (mode 'add') or multiplied (mode 'scale')."""
    hours = []
    for h in inputs.hours:
        v = getattr(h, field)
        if v is not None:
            v = v + amount if mode == "add" else max(0.0, v * amount)
        hours.append(h.model_copy(update={field: v}))
    return inputs.model_copy(update={"hours": hours})


def what_would_change(inputs: Inputs, rec: Recommendation, th: dict, version: str | None) -> list[Change]:
    """For each threshold, the exact change in its field that would cross (or un-cross) it. Each candidate
    is verified by re-running the engine; the smallest posture-changing change per field and direction is kept."""
    best: dict[tuple[str, int], tuple[float, Change]] = {}
    for c in rec.checks:
        if c.peak is None:
            continue
        # +1 moves the value up. Cross an uncrossed '>=' threshold by going up, un-cross it by going down;
        # the reverse for '<=' thresholds such as wind chill.
        worse = 1 if th["field_rules"][c.rule_id]["op"] == ">=" else -1
        direction = -worse if c.crossed else worse
        target = c.threshold + direction * 0.01
        limit = th["confidence"]["what_if_range_multiple"] * th["confidence"]["borderline_margin"][c.field]
        if c.field in SCALED:
            if c.peak <= 0 or target <= 0 or not 0.5 <= target / c.peak <= 2:
                continue
            factor = target / c.peak
            trial = perturbed(inputs, SCALED[c.field], factor, "scale")
            how = f"{(factor - 1) * 100:+.0f}% (12-hour total {target:.2f} in)"
            size = abs(factor - 1)
        else:
            delta = target - c.peak
            if abs(delta) > limit:
                continue
            trial = perturbed(inputs, SHIFTED.get(c.field, c.field), delta, "add")
            how = f"{delta:+.2f} {c.unit} (peak {target:.2f})"
            size = abs(delta)
        r = evaluate(trial, th, version, explore=False)
        if r.posture == rec.posture:
            continue
        key = (c.field, direction)
        verb = "dropping below" if c.crossed else "crossing"
        change = Change(what=f"{FIELD_NAMES[c.field]} {how}, {verb} {c.rule_id} ({c.threshold} {c.unit})",
                        posture=r.posture, detail=r.posture_label)
        if key not in best or size < best[key][0]:
            best[key] = (size, change)
    out = [v[1] for v in best.values()]

    watch_up = th["watch_events"]["upgrades_to"]
    for a in inputs.alerts:
        if a.event in watch_up:
            upgraded = [x.model_copy(update={"event": watch_up[a.event], "certainty": "Likely"}) if x.id == a.id else x
                        for x in inputs.alerts]
            r = evaluate(inputs.model_copy(update={"alerts": upgraded}), th, version, explore=False)
            if r.posture != rec.posture:
                out.append(Change(what=f"{a.event} upgraded to {watch_up[a.event]}", posture=r.posture,
                                  detail=r.posture_label))
    for f in rec.decisive:
        if f.kind == "alert":
            remaining = [x for x in inputs.alerts if x.id != f.alert_id]
            r = evaluate(inputs.model_copy(update={"alerts": remaining}), th, version, explore=False)
            if r.posture != rec.posture:
                out.append(Change(what=f"{f.alert_event} cancelled or expired", posture=r.posture,
                                  detail=r.posture_label))
    return out
