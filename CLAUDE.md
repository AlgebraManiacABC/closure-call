# Closure Call: project context

Hack Midwest 2026 project (Kansas City, Oct 10-11). This file records the decisions made
during planning and what has been built so far (see Repo state).

## Deadlines and rules

- Project checkpoint: Saturday Oct 10, 7:00 PM Central.
- Submissions due: Sunday Oct 11, 8:00 AM Central. No changes after that.
- Pitch: 3 minutes, live demo only, no slide decks. Be signed in already.
- All code must be written during the event. Third-party libraries are allowed.
- One project per team, up to five people, all present in person.
- General judging: value and usefulness, innovation, completeness as a working MVP.
  Code quality is not judged.

## Challenges targeted

One project entered in three challenges:

1. USDA "Closure Call" (non-cash prizes: recommendation letters from USDA's CTO and CDO,
   recognition, networking).
2. AWS, Best Use of Strands Agents SDK ($2,500).
3. Akamai Cloud AI Challenge ($2,500). Judged on impact, use of Akamai resources,
   technical execution, creativity. Must be more than a basic chatbot.

Not targeting Flex (FinTech) or DevRev.

## What the USDA brief requires

Problem: when severe weather threatens a federal facility, help a human decide whether to
close it, and notify the right employees, quickly, consistently, and with a record of why.
The system recommends; a person decides.

Five deliverables:

1. Working system with a live demo on real NWS data (not a mock feed).
2. Recommendation with reasoning: posture, every input with source and timestamp, which
   threshold was crossed and by how much, confidence, what would change the
   recommendation, and a one-paragraph plain-language rationale.
3. Notification package: recipient list with the logic for who is included and excluded,
   a drafted message, and channel routing.
4. Decision record: what was known, when, and what was decided, reconstructable later.
   The brief says most teams skip this and an agency would care about it most.
5. Threshold defensibility: every cutoff needs a stated source (NWS severity fields,
   state DOT thresholds, OPM operating-status categories).

The brief says one facility profile and two or three hazards done well beats broad and
shallow.

## Scope decisions

- Facility: one synthetic USDA Service Center, about 40 employees. Its location can be
  re-pinned to wherever alerts are active so the live demo always has real weather.
- Keep the demo framed as a USDA facility. The facility model can be generic underneath.
- Hazards: flooding, high wind, winter weather.
- Supplementary signal: USGS river gauges (free, no key).
- Postures: normal operations, delayed arrival, early dismissal, unscheduled leave or
  telework, full closure.
- Roster fields: assignment, on detail elsewhere, already teleworking, essential
  personnel, telework eligibility, accessibility needs.

## Architecture decisions

Core principle: plain code decides, AI investigates and explains.

- Scheduled plain code fetches NWS and USGS data and stores the raw responses. No model
  is involved in routine collection.
- A deterministic rules engine picks the posture from structured fields. Thresholds live
  in a config file, each next to its source. Identical inputs must always give the same
  posture.
- A Strands agent runs only when the result is not "normal". It:
  - extracts details from the free text of NWS alerts, keeping the quoted sentence as
    evidence;
  - does follow-up lookups that depend on the hazard (upstream gauges and crest forecast
    for floods, hourly temperatures near commute time for winter storms);
  - re-runs the rules engine with altered inputs to report what would change the posture;
  - writes the rationale paragraph and answers manager questions from recorded inputs.
- Structured NWS fields stay the primary drivers. Anything the agent extracted is shown to
  the manager with its quoted source.
- A roster filter in plain code sorts each employee into notify or skip, with a reason.
- A notification agent drafts a separate message per employee group and routes by
  urgency (text for urgent, email otherwise). Sending is simulated; the demo shows an
  outbox.
- Decision record: append-only table holding raw inputs with source and timestamp, the
  rule that fired, and the manager's approve or override with a written reason.
- Nothing is sent without the manager's approval.

## Users and screens

- Facility manager (main user): phone-first web page. The app prompts them by text or
  email with a link when an alert is issued or upgraded, a threshold is crossed, the
  recommended posture changes, or a decision deadline is near. It stays quiet when the
  posture is normal and unchanged.
- Employees: recipients only. They get a message and never log in.
- Reviewers: read the decision history on a desktop.
- One responsive web app, three screens: manager decision page, message outbox, decision
  history. No login for the demo; use a role switcher.
- A "check now" button runs the evaluation on demand for the demo.

## Stack

- Python 3.12, FastAPI, Strands Agents SDK (checked against release 1.59).
- Hosting: one Akamai Cloud Linode running Docker Compose. Not Kubernetes.
- Database: Akamai Managed PostgreSQL for the decision record. It takes five to eight
  minutes to become reachable and only accepts connections from addresses on its allow
  list, so create it early and add both the laptop and the Linode.
- Inference: Akamai's hosted workshop endpoint.

## Akamai inference endpoint (verified)

- Base URL: https://ws.akamai-workshops.com/v1, OpenAI-compatible, team key in `.env`.
- Chat alias: `hackathon-chat`. Backing model reported as `Qwen/Qwen3.8-27B-FP8`.
- `scripts/check_endpoint.py` passed on Oct 10: a Strands agent using
  `strands.models.openai.OpenAIModel` called one tool correctly through this endpoint.
  Multi-step, multi-tool runs with structured output also work (see `app/agent.py`).
- Limits: 32,768-token context; output defaults to 512 tokens, so always set
  `max_tokens` (maximum 4,096); 60 requests and 100,000 tokens per minute shared by the
  whole team.
- Only chat completions, embeddings and image generation exist. No Responses API.
- `tool_choice="none"` is unreliable here. To forbid tools, omit `tools`.
- Thinking mode is off by default and uses output budget when on. Leave it off.
- Trim raw NWS responses to the relevant fields before passing them to the agent.
- Keep each agent to a few tools with short tool outputs.

Reference repo: https://github.com/akamai-developers/akamai-workshop-hackmidwest
(its sample event documents are fictional, not the real hackathon rules).

## Data sources

- NWS API (api.weather.gov): public, no key. Active alerts, forecasts and observations by
  point or zone. Alerts carry structured severity, certainty and urgency fields.
- USGS water services: public, no key. River gauge readings and flood stage.

Verified Oct 10 (clients in `app/sources/`, check with `scripts/check_sources.py`):
- Alert `parameters` almost never carry magnitudes (gust mph, snow inches). Those come
  from the free text, which is the agent's job, or from the gridpoint forecast.
- `/gridpoints/{wfo}/{x},{y}` gives numeric series: windGust, snowfallAmount,
  iceAccumulation, windChill, potentialOfNNmphWindGusts. Units are km/h, mm and degC,
  converted to mph, in and F in `nws.py`. This is the structured source for magnitude
  thresholds.
- NOAA NWPS (api.water.noaa.gov/nwps/v1) is the main flood-gauge source: NWS-defined
  action/minor/moderate/major stages per gauge, current category, crest forecast, upstream
  gauge. USGS IV is a raw cross-check. The USGS legacy API has no flood stages.
- `/alerts/active?point=` resolves zones and polygons itself. Wind and winter alerts are
  zone-based (no geometry); flood warnings have polygons along rivers, so a town next to a
  flooding river may have no alert. `scripts/check_sources.py --hotspots` lists re-pin
  coordinates per hazard that do fall inside an alert.
- The local venv is Python 3.11.9, not 3.12.

## Planned timeline

1. Data layer: NWS client, USGS client, synthetic facility and roster.
2. By the 7 PM checkpoint: thin end-to-end version live on Akamai.
3. Evening: notification logic, decision record, threshold write-up.
4. Overnight: polish and rehearse the three-minute demo.

## Repo state

- `scripts/check_endpoint.py`: endpoint and Strands check (passing).
- `app/sources/`: NWS, NWPS/USGS and hotspot clients. Each fetch returns `Fetched` (raw
  body, URL, UTC time) for the decision record. `scripts/check_sources.py` exercises them.
- `data/facility.json`: the synthetic Service Center (hours, commute windows, decision
  deadline, federal holidays, three re-pin presets: `flood_iowa`, `wind_carolina`,
  `winter_montana`). `data/roster.json`: 40 fictional people, generated by
  `scripts/make_roster.py` (fixed seed; hand-written edge cases E001-E019).
- `app/facility.py`: loads both, re-pins, finds the next operating day, and gives each
  person's status for a day (office, telework, leave, detail elsewhere, detailed in) with a
  reason. Notify/skip decisions are not in here; they belong to the roster filter.
  `scripts/show_facility.py` prints it.
- The demo is on Sunday Oct 11 and Mon Oct 12 is Columbus Day, so the next operating day is
  Tue Oct 13. Pass an explicit day to evaluate a different one.
- `data/thresholds.json`: every cutoff with its source; values that are our own choices are
  marked `kind: assumption` and say what would replace them. Its hash is the rules version.
- `app/rules.py`: the pure rules engine, `evaluate(Inputs) -> Recommendation`. Each hour of
  the day gets a level per hazard (0 none, 1 advisory-level, 2 warning-level) from forecast
  fields and NWS alerts. A timing step maps warning-level hours to delay (2-4 h), early
  dismissal (2 h before the hazard) or closure. Advisory-level hours alone give unscheduled
  leave. Snow, ice and winter alerts stay hazardous for 3 h after they end (road recovery).
  It also produces confidence with reasons, and "what would change", computed as the exact
  change needed to cross each threshold, verified by re-running the engine.
- `app/collect.py`: all network access. It builds `Inputs` (hourly series from noon the day
  before, alerts, gauge with NWS impact statements, sources with URL and fetch time).
  `scripts/evaluate.py --preset X --day YYYY-MM-DD|today|tomorrow [--json f]` runs both.
  `tests/test_rules.py` has synthetic tests; run with `python tests/test_rules.py`.
- Flood stages are held for 12 h (`flood_recession_hours`, an assumption) so a falling river
  does not clear the hazard the same hour.
- Live results on Oct 10: flood_iowa on Sun gives a 2-hour delay (major stage until Sat
  evening, plus the 12 h hold); winter_montana on Mon Oct 12 gives a 3-hour delay;
  wind_carolina on Sun gives a 2-hour delay. The wind and winter results come from alerts
  alone, because the forecast at the pin is below warning level, so their confidence is
  medium.
- `app/agent.py`: the Strands agent (`investigate`) runs only for non-normal postures. It has
  4 tools: read_alert, record_evidence (plain code rejects any quote not found word for word in
  the alert), what_if (re-runs the engine), and lookup (commute_hours, observation,
  upstream_river). It returns an `AgentReport` with the rationale (structured output),
  caveats, evidence, lookups and what-ifs. If the model fails, it falls back to the engine's
  template summary. `answer_question` answers manager questions from recorded inputs only
  (no network). Alerts get short ids (A1...) so the model never copies URNs.
  `app/llm.py` builds the model. `scripts/brief.py --preset X --day D --ask "..."` runs the
  whole pipeline.
- Agent runs measured Oct 10: about 40 s, 15-19k tokens, 8-11 tool calls, every quote
  verified. Multi-step, multi-tool runs and structured output work on this endpoint. With
  the team's 100k tokens/min, that is about 5 investigations a minute.
- `Inputs` now also carries alert text, issue time and area, hourly temperature, and the
  facility elevation (from the gridpoint). The engine ignores these; the agent uses them.
- `tests/test_agent_tools.py`: quote verification and what_if, no model needed.
- `.env.example`, `requirements.txt`, `.gitignore`, `README.md`.
- `.env` holds the team key and is gitignored. Never commit it or print the key.

## Open questions

- Team size and who does what.
- Whether USDA judges accept simulated notification sending (worth asking at their
  workshop), and which of the five deliverables they weigh most.
- Whether AWS judges mind a non-AWS model provider behind Strands.
- Exact threshold values and their sources have not been chosen yet.
- The Akamai account, Linode and Postgres database have not been created yet.
