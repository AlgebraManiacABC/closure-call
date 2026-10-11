"""Notification package: who to notify and why, what each group is told, and how it is routed.

Plain code decides everything that matters:
  - who is notified or skipped, with a reason for each person (roster filter);
  - which group each person is in, and the facts that group must be told for the decided posture;
  - personal notes (early shift, flood-prone commute, accessibility needs, detailed in);
  - channel routing (text and email for urgent postures, preferred channel otherwise).
The model only drafts the wording of one SMS and one email per group. Code checks each draft for
length and for the key facts; a draft that fails is replaced by a code template.

Nothing is sent from here. Sending is simulated and needs the manager's approval (see record.py).
"""
import re
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel
from strands import Agent

from .facility import DayStatus, Employee, Facility, roster_for_day
from .llm import make_model
from .rules import Inputs, Recommendation

URGENT = {"closure", "delayed_arrival", "early_dismissal"}
SMS_MAX = 320  # two SMS segments


class Decided(BaseModel):
    """The posture the manager actually decided (approve or override), not just the recommendation."""
    posture: str
    label: str
    delay_hours: int | None = None
    dismissal_time: str | None = None  # HH:MM local
    action: str = "approve"
    decided_by: str = ""
    reason: str = ""  # the manager's written reason (required for overrides)
    recommended_label: str = ""


class Recipient(BaseModel):
    employee_id: str
    name: str
    title: str
    agency: str
    notify: bool
    group: str | None  # essential, teleworking, office_telework_ready, office_no_telework
    reasons: list[str]  # why notified or skipped
    personal_notes: list[str] = []
    channels: list[str] = []  # sms, email
    urgent: bool = False
    phone: str = ""
    email: str = ""


class GroupDraft(BaseModel):
    group: str
    sms: str
    email_subject: str
    email_body: str


class Drafts(BaseModel):
    """Structured output from the drafting model."""
    decision_summary: str = ""
    groups: list[GroupDraft]


class GroupMessage(BaseModel):
    group: str
    description: str
    facts: str  # what this group must be told, decided in code
    count: int
    sms: str
    email_subject: str
    email_body: str
    source: str  # "agent" or "template"
    problems: list[str] = []  # why an agent draft was replaced


class NotificationPackage(BaseModel):
    facility_label: str
    day: date
    decided: Decided
    reason_phrase: str
    urgent: bool
    send_by: datetime | None
    recipients: list[Recipient]
    groups: list[GroupMessage]
    drafting: str  # how the drafts were produced
    counts: dict[str, int]
    summary: str = ""  # the decision summary for the manager's email to leadership
    summary_source: str = ""  # "agent" or "template"
    summary_problems: list[str] = []


class OutboxMessage(BaseModel):
    employee_id: str
    name: str
    channel: str
    to: str
    subject: str = ""
    body: str
    group: str
    status: str = "simulated"  # nothing is actually sent in the demo


GROUP_DESCRIPTIONS = {
    "essential": "Emergency (essential) personnel",
    "teleworking": "Scheduled to telework that day",
    "office_telework_ready": "In the office that day, telework-ready",
    "office_no_telework": "In the office that day, not telework-eligible",
}


# ---------- helpers ----------

def clock(t: time | str) -> str:
    """12-hour time for messages, e.g. 1:30 PM."""
    if isinstance(t, str):
        t = time.fromisoformat(t)
    return datetime.combine(date.today(), t).strftime("%I:%M %p").lstrip("0")


def add_hours(t: time, hours: int) -> time:
    return (datetime.combine(date.today(), t) + timedelta(hours=hours)).time()


def reason_phrase(inputs: Inputs, rec: Recommendation) -> str:
    """Short, plain cause for messages, from the engine's decisive finding."""
    for f in rec.decisive:
        if f.kind == "alert":
            return f"an NWS {f.alert_event}"
        if f.hazard == "flood" and inputs.gauge:
            return f"flooding on the {inputs.gauge.name}"
        return {"wind": "high winds", "winter": "snow and ice", "flood": "flooding",
                "tropical": "a tropical storm"}.get(f.hazard, "severe weather")
    return "severe weather"


def group_of(s: DayStatus) -> str:
    e = s.employee
    if e.essential:
        return "essential"
    if s.location == "telework":
        return "teleworking"
    return "office_telework_ready" if e.telework_eligible else "office_no_telework"


def group_facts(d: Decided, group: str, leave_option: bool, facility: Facility) -> str | None:
    """What each group must be told for the decided posture; None means this group is not notified.
    Wording follows OPM's operating-status categories."""
    open_t = clock(facility.open)
    if d.posture == "closure":
        return {
            "essential": "The office is closed to the public and non-emergency staff. As emergency personnel, "
                         "report as scheduled unless your supervisor tells you otherwise.",
            "teleworking": "The office is closed. Telework-ready employees work from home for their full "
                           "schedule. Do not come to the office.",
            "office_telework_ready": "The office is closed. Do not come in. Telework-ready employees work from "
                                     "home for their full schedule.",
            "office_no_telework": "The office is closed. Do not come in. Your scheduled hours are covered by "
                                  "weather and safety leave under agency policy.",
        }[group]
    if d.posture == "delayed_arrival":
        n = d.delay_hours
        option = (" You may instead take unscheduled leave or unscheduled telework; tell your supervisor."
                  if leave_option else "")
        option_no_tw = " You may instead take unscheduled leave; tell your supervisor." if leave_option else ""
        return {
            "essential": f"The office opens on a {n}-hour delay. As emergency personnel, report at your normal "
                         "time unless your supervisor tells you otherwise.",
            "teleworking": f"The office opens on a {n}-hour delay for commuters. On a scheduled telework day, "
                           "start work at your normal time.",
            "office_telework_ready": f"The office opens on a {n}-hour delay: arrive up to {n} hours after your "
                                     f"normal start (an {open_t} start becomes {clock(add_hours(facility.open, n))})."
                                     + option,
            "office_no_telework": f"The office opens on a {n}-hour delay: arrive up to {n} hours after your normal "
                                  f"start (an {open_t} start becomes {clock(add_hours(facility.open, n))})."
                                  + option_no_tw,
        }[group]
    if d.posture == "early_dismissal":
        t = clock(d.dismissal_time)
        return {
            "essential": f"Non-emergency staff leave at {t}. As emergency personnel, remain on duty unless your "
                         "supervisor releases you.",
            "teleworking": None,  # no commute to protect
            "office_telework_ready": f"Leave the office by {t} to get home before conditions worsen. "
                                     "Telework-ready employees finish the day from home.",
            "office_no_telework": f"Leave the office by {t} to get home before conditions worsen. Remaining "
                                  "hours are covered by weather and safety leave under agency policy.",
        }[group]
    if d.posture == "unscheduled_leave":
        return {
            "essential": None,  # no change for emergency personnel
            "teleworking": None,  # already home
            "office_telework_ready": "The office is open. You may take unscheduled leave or unscheduled "
                                     "telework without prior approval; tell your supervisor before your start time.",
            "office_no_telework": "The office is open. You may take unscheduled leave without prior approval; "
                                  "tell your supervisor before your start time.",
        }[group]
    return None


def personal_notes(s: DayStatus, d: Decided, hazards: set[str], leave_option: bool, facility: Facility) -> list[str]:
    e: Employee = s.employee
    notes = []
    if s.detailed_in:
        notes.append(f"You are on detail here from {e.duty_station}; this applies to you while assigned to this "
                     "office. Your home office may also contact you.")
    if d.posture == "delayed_arrival" and e.shift_start != facility.open and not e.essential \
            and s.location == "office":
        notes.append(f"Your shift normally starts at {clock(e.shift_start)}, so your delayed start is "
                     f"{clock(add_hours(e.shift_start, d.delay_hours))}.")
    if e.commute_flood_prone and ({"flood", "tropical"} & hazards) and s.location == "office":
        notes.append("Your usual route crosses flood-prone roads. Never drive through flooded roads (Turn Around, "
                     "Don't Drown)" + ("; use unscheduled leave or telework if your route is affected." if leave_option
                                       else "; call your supervisor if your route is affected."))
    types = {a["type"] for a in e.accessibility_needs}
    if s.location == "office" and d.posture != "closure":
        if "mobility" in types and "winter" in hazards:
            notes.append("The parking lot and ramp may not be fully cleared. Call your supervisor before you "
                         "travel if access is a concern.")
        if "vision" in types:
            notes.append("If your ride is affected, call your supervisor about leave options.")
        if "medical" in types and "winter" in hazards:
            notes.append("Cold exposure is a risk today; limit time outdoors and call your supervisor if needed.")
    return notes


# ---------- roster filter ----------

def build_recipients(facility: Facility, roster: list[Employee], day: date, d: Decided,
                     rec: Recommendation) -> list[Recipient]:
    hazards = {f.hazard for f in rec.decisive}
    urgent = d.posture in URGENT
    out = []
    for s in roster_for_day(roster, facility, day):
        e = s.employee
        base = dict(employee_id=e.id, name=e.name, title=e.title, agency=e.agency, phone=e.phone, email=e.email)
        if d.posture == "normal":
            out.append(Recipient(**base, notify=False, group=None, reasons=["Office open on normal schedule; no notice needed."]))
            continue
        if e.id == facility.manager_employee_id:
            out.append(Recipient(**base, notify=False, group=None, reasons=[
                "Facility manager who made the decision; already informed."]))
            continue
        if s.location.startswith("detail"):
            out.append(Recipient(**base, notify=False, group=None, reasons=[
                s.reason + " Their operating status is set by that location."]))
            continue
        if s.location == "leave":
            out.append(Recipient(**base, notify=False, group=None, reasons=[
                "On approved leave that day; the posture does not change their day."]))
            continue
        g = group_of(s)
        facts = group_facts(d, g, rec.unscheduled_leave_option, facility)
        if facts is None:
            why = {"teleworking": "Teleworking that day with no commute; the posture does not change their day.",
                   "essential": "Emergency personnel; their reporting does not change for this posture."}[g]
            out.append(Recipient(**base, notify=False, group=g, reasons=[why]))
            continue
        reasons = [s.reason]
        if e.essential:
            reasons.append("Emergency personnel: reporting requirements differ, so they get their own message.")
        if s.detailed_in:
            reasons.append("Detailed in from another office: not on this building's own roster, but works here this week.")
        if not e.telework_eligible and g == "office_no_telework":
            reasons.append("Not telework-eligible: told about leave rather than telework.")
        notes = personal_notes(s, d, hazards, rec.unscheduled_leave_option, facility)
        channels = ["sms", "email"] if urgent or notes else [e.preferred_channel]
        if any(a["type"] == "hearing" for a in e.accessibility_needs):
            reasons.append("Deaf: text and email only, never voice calls.")
        out.append(Recipient(**base, notify=True, group=g, reasons=reasons, personal_notes=notes,
                             channels=channels, urgent=urgent))
    return out


# ---------- drafting ----------

DRAFT_PROMPT = """You write up a facility manager's weather decision after it has been made.
Plain code has already decided the posture, who is in each group, and the facts each group must be told.
First write decision_summary: ONE paragraph of at most 110 words the manager can paste into an email to
leadership. State the decision and the day, who decided and whether they approved the recommendation or
overrode it (quote their reason if one is given), the cause, and who is being notified. Use only the facts
given; no caveats, no hedging, no new numbers. Refer to people by name, never with he, she, his or her.
Then write one SMS and one email per group. Rules:
- Keep every fact for the group exactly; do not add instructions, numbers, times or promises.
- SMS: under 300 characters, starts with the facility and day, includes the key time or delay.
- Email: a clear subject naming the status and day; body of 3-5 short sentences: what is happening and
  why (one clause), what THIS group should do, and who to contact. Sign it from the facility manager.
- Plain, calm language. Times like 10:00 AM. No emoji, no rule ids."""


def _check(text: str, d: Decided, day: date, limit: int | None = None) -> list[str]:
    problems = []
    if limit and len(text) > limit:
        problems.append(f"too long ({len(text)} chars)")
    if day.strftime("%A") not in text and day.strftime("%a") not in text:
        problems.append("missing the day")
    if d.posture == "delayed_arrival" and f"{d.delay_hours}-hour" not in text and f"{d.delay_hours} hour" not in text:
        problems.append("missing the delay length")
    if d.posture == "early_dismissal" and clock(d.dismissal_time) not in text:
        problems.append("missing the dismissal time")
    if d.posture == "closure" and "closed" not in text.lower():
        problems.append("does not say the office is closed")
    return problems


def template(group: str, facts: str, d: Decided, facility_label: str, day: date, why: str,
             manager: Employee) -> GroupDraft:
    head = f"{facility_label}, {day:%A %b %d}"
    sms = f"{head}: {facts} Questions: {manager.name} {manager.phone}."
    if len(sms) > SMS_MAX:
        sms = f"{head}: {d.label}. Details by email. Questions: {manager.phone}."
    body = (f"{d.label} for {facility_label} on {day:%A, %B %d}, because of {why}.\n\n{facts}\n\n"
            f"Questions: {manager.name}, {manager.phone}.\n\n{manager.name}\nFacility manager")
    return GroupDraft(group=group, sms=sms, email_subject=f"{d.label}: {day:%A %b %d}", email_body=body)


def summary_template(d: Decided, facility_label: str, day: date, why: str, timing: str, counts: dict) -> str:
    """Code-written decision summary, used when the AI is not available or its summary fails the checks."""
    who = d.decided_by.split(" (@")[0] or "The facility manager"
    if d.action == "override":
        s = (f"{who} overrode the recommended status ({d.recommended_label}) and set {facility_label} to "
             f"{d.label} for {day:%A, %B %d}. Reason given: \"{d.reason}\" ")
    else:
        s = f"{who} approved the recommended status for {facility_label} on {day:%A, %B %d}: {d.label}. "
    s += f"Cause: {why}. {timing} "
    return s + (f"Notices go to {counts['notify']} employees; {counts['skip']} are not notified "
                "(on detail elsewhere, on leave, or not affected).")


def draft_groups(needed: dict[str, str], d: Decided, facility_label: str, day: date, why: str,
                 manager: Employee, context: str = "") -> tuple[list[GroupMessage], str, str]:
    """One AI request after the decision: the decision summary and each group's drafts, all checked in code;
    templates replace anything that fails. Returns (groups, status, the AI's summary or "")."""
    agent_drafts: dict[str, GroupDraft] = {}
    summary = ""
    status = "template (agent not used)"
    if needed:
        brief = "\n".join(f"- group {g} ({GROUP_DESCRIPTIONS[g]}): {facts}" for g, facts in needed.items())
        task = (f"Facility: {facility_label}. Day: {day:%A %B %d}. Status: {d.label}. Cause: {why}.\n{context}\n"
                f"Contact named at the end of the employee notices only (not the decider): {manager.name}, "
                f"{manager.phone}.\n"
                f"Groups and the facts each must be told:\n{brief}")
        try:
            agent = Agent(model=make_model(max_tokens=3000, temperature=0.3), system_prompt=DRAFT_PROMPT,
                          callback_handler=None)
            res = agent(task, structured_output_model=Drafts)
            if res.structured_output:
                agent_drafts = {g.group: g for g in res.structured_output.groups}
                summary = res.structured_output.decision_summary.strip()
            status = "agent drafts, validated in code"
        except Exception as e:
            status = f"templates (agent failed: {type(e).__name__})"
    out = []
    for g, facts in needed.items():
        tpl = template(g, facts, d, facility_label, day, why, manager)
        a = agent_drafts.get(g)
        problems = []
        if a is None:
            problems = ["no agent draft"] if agent_drafts else []
        else:
            problems = [f"sms {p}" for p in _check(a.sms, d, day, SMS_MAX)] + \
                       [f"email {p}" for p in _check(a.email_subject + " " + a.email_body, d, day)]
        use = a if a is not None and not problems else tpl
        out.append(GroupMessage(group=g, description=GROUP_DESCRIPTIONS[g], facts=facts, count=0, sms=use.sms,
                                email_subject=use.email_subject, email_body=use.email_body,
                                source="agent" if use is a else "template", problems=problems))
    return out, status, summary


def build_package(facility: Facility, roster: list[Employee], inputs: Inputs, rec: Recommendation,
                  d: Decided, use_agent: bool = True) -> NotificationPackage:
    recipients = build_recipients(facility, roster, inputs.day, d, rec)
    manager = next(e for e in roster if e.id == facility.manager_employee_id)
    why = reason_phrase(inputs, rec)
    groups_needed = {}
    for r in recipients:
        if r.notify and r.group not in groups_needed:
            groups_needed[r.group] = group_facts(d, r.group, rec.unscheduled_leave_option, facility)
    label = facility.display_name
    counts = {"notify": sum(r.notify for r in recipients), "skip": sum(not r.notify for r in recipients),
              "sms": sum("sms" in r.channels for r in recipients if r.notify),
              "email": sum("email" in r.channels for r in recipients if r.notify),
              "personal_notes": sum(bool(r.personal_notes) for r in recipients)}
    decider = d.decided_by.split(" (@")[0] or "The facility manager"
    context = (f"Decided by: {decider}, who {'approved the recommendation' if d.action == 'approve' else 'overrode the recommendation'}. "
               f"Recommended: {d.recommended_label or rec.posture_label}. "
               f"Decided: {d.label}. Reason given: {d.reason or 'none'}. Timing: {rec.timing_explanation} "
               f"Notifying {counts['notify']} employees; {counts['skip']} not notified.")
    tpl_summary = summary_template(d, label, inputs.day, why, rec.timing_explanation, counts)
    ai_summary = ""
    if use_agent:
        groups, status, ai_summary = draft_groups(groups_needed, d, label, inputs.day, why, manager, context)
    else:
        groups = [GroupMessage(group=g, description=GROUP_DESCRIPTIONS[g], facts=f, count=0, source="template",
                               **template(g, f, d, label, inputs.day, why, manager).model_dump(exclude={"group"}))
                  for g, f in groups_needed.items()]
        status = "templates (agent not requested)"
    summary_problems = _check(ai_summary, d, inputs.day, 900) if ai_summary else []
    if ai_summary and re.search(r"\b(he|she|his|her|hers|him)\b", ai_summary, re.I):
        summary_problems.append("uses a gendered pronoun for a person")
    verb = "overr" if d.action == "override" else "approv"
    if ai_summary and not re.search(re.escape(decider) + r"[^.]{0,30}?\b" + verb, ai_summary, re.I):
        summary_problems.append(f"does not say that {decider} {'overrode' if d.action == 'override' else 'approved'} it")
    use_ai = bool(ai_summary) and not summary_problems
    for gm in groups:
        gm.count = sum(1 for r in recipients if r.notify and r.group == gm.group)
    if d.posture in ("closure", "delayed_arrival"):
        send_by = inputs.at(inputs.decide_by)
    elif d.posture == "early_dismissal":
        send_by = inputs.at(time.fromisoformat(d.dismissal_time)) - timedelta(hours=1)
    else:
        send_by = None
    return NotificationPackage(facility_label=label, day=inputs.day, decided=d, reason_phrase=why,
                               urgent=d.posture in URGENT, send_by=send_by, recipients=recipients, groups=groups,
                               drafting=status, counts=counts, summary=ai_summary if use_ai else tpl_summary,
                               summary_source="agent" if use_ai else "template", summary_problems=summary_problems)


def outbox(pkg: NotificationPackage) -> list[OutboxMessage]:
    """One message per recipient per channel: the group draft plus that person's notes."""
    by_group = {g.group: g for g in pkg.groups}
    msgs = []
    for r in pkg.recipients:
        if not r.notify:
            continue
        g = by_group[r.group]
        for ch in r.channels:
            if ch == "sms":
                body = g.sms + ("" if not r.personal_notes else " " + " ".join(r.personal_notes))
                if len(body) > 480:  # keep texts to three segments; full notes go by email
                    body = g.sms + " Personal note sent by email."
                msgs.append(OutboxMessage(employee_id=r.employee_id, name=r.name, channel="sms", to=r.phone,
                                          body=body, group=r.group))
            else:
                notes = "" if not r.personal_notes else "For you specifically:\n- " + "\n- ".join(r.personal_notes) + "\n\n"
                msgs.append(OutboxMessage(employee_id=r.employee_id, name=r.name, channel="email", to=r.email,
                                          subject=g.email_subject, body=f"{r.name.split()[0]},\n\n{notes}{g.email_body}",
                                          group=r.group))
    return msgs
