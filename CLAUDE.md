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
  deadline, federal holidays, and one default starting pin (Grand Mound, IA). Presets were
  removed Oct 10; locations are chosen live from /locations). `data/roster.json`: 40 fictional people, generated by
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
  `scripts/evaluate.py [--lat N --lon N] --day YYYY-MM-DD|today|tomorrow [--json f]` runs both.
  `tests/test_rules.py` has synthetic tests; run with `python tests/test_rules.py`.
- Flood stages are held for 12 h (`flood_recession_hours`, an assumption) so a falling river
  does not clear the hazard the same hour.
- Live results on Oct 10 (at the old preset locations, since removed): flood_iowa on Sun gives a 2-hour delay (major stage until Sat
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
  `app/llm.py` builds the model. `scripts/brief.py [--lat N --lon N] --day D --ask "..."` runs the
  whole pipeline.
- Agent runs measured Oct 10: about 40 s, 15-19k tokens, 8-11 tool calls, every quote
  verified. Multi-step, multi-tool runs and structured output work on this endpoint. With
  the team's 100k tokens/min, that is about 5 investigations a minute.
- `Inputs` now also carries alert text, issue time and area, hourly temperature, and the
  facility elevation (from the gridpoint). The engine ignores these; the agent uses them.
- `tests/test_agent_tools.py`: quote verification and what_if, no model needed.
- `app/record.py`: the decision record (SQLAlchemy Core; SQLite by default, Postgres via
  `DATABASE_URL`). The `ledger` table is append-only and hash-chained: one row per event
  (evaluation, agent_report, question, decision). `entry` holds the exact canonical JSON and
  `hash = sha256(prev_hash + entry)`. `raw_fetches` holds every raw API response gzipped
  (about 40 KB per evaluation) with its sha256, listed in the evaluation entry. `rule_sets`
  holds each thresholds version. Triggers block UPDATE and DELETE (and TRUNCATE on Postgres).
  `verify_chain()` recomputes every hash. `replay(id)` re-runs the engine on the recorded
  inputs and rules and must match exactly. Overrides require a posture and a reason of at
  least 10 characters.
- The rules version is the sha256 of thresholds.json with line endings normalized, so it
  matches on Windows and Linux.
- `scripts/brief.py ... --record --decide approve|override --by NAME [--reason ...]` records a
  full run. `scripts/history.py [--show ID | --verify | --replay ID]` reads it back.
  `tests/test_record.py` covers the lifecycle, override validation, the triggers, and tamper
  detection.
- Akamai Managed PostgreSQL 18.6 is live and holds the real decision record (`DATABASE_URL`
  in `.env`; port 18688, database `defaultdb`, TLS required). Tables and triggers were
  created Oct 10. A smoke test in a throwaway database on the same server passed: lifecycle,
  identical replay, 12 concurrent writers with an unbroken chain, UPDATE/DELETE/TRUNCATE all
  blocked, and tampering with the trigger disabled detected by `verify_chain`.
- `normalize_url` accepts the `postgres://` URL Akamai shows, switches it to
  `postgresql+psycopg`, and adds `sslmode=require`.
- psycopg treats `%` in raw SQL as a placeholder, so the trigger message is built with `||`.
- The laptop's VPN broke DNS for `*.akamaidb.net`; turn it off to reach the database. The
  Linode's IP must be added to the database allow list when it exists.
- `app/notify.py`: the notification package, built from the manager's DECISION (not the
  recommendation). Plain code decides:
  - who is notified or skipped, with reasons (skipped: the facility manager, people on detail
    elsewhere, people on leave, and groups the posture does not affect);
  - the group: essential, teleworking, office_telework_ready or office_no_telework;
  - the facts each group must be told per posture (OPM categories; closure means
    telework-ready staff work from home and others get weather and safety leave);
  - personal notes (detailed in, early shift under a delay, mobility/vision/cold-sensitivity);
    during a flood or storm surge every group that still commutes gets "Turn Around, Don't Drown"
    (`notify.travels_during_flood`; AI drafts missing it are replaced);
  - routing: sms+email for closure, delay and early dismissal; preferred channel for the
    unscheduled-leave option.
  One Strands structured-output call drafts an SMS and an email per group. `_check` rejects
  drafts missing the day, delay length, dismissal time or "closed", or over 320 characters,
  and swaps in a code template.
- Record events: `notification_package` (draft, tied to the decision hash) and
  `notifications_sent` (manager approval; one simulated outbox message per person per
  channel). `record_send` refuses unless a package exists for the latest decision.
- `scripts/notify.py --evaluation ID [--send --by NAME] [--no-agent] [--db local]`.
  `--db local` (also on brief.py and history.py) uses the SQLite file, so development runs
  stay out of the Akamai record. `tests/test_notify.py` covers each posture's rules.
- Live Montana run (2-hour delay override): 36 notified, 4 skipped, 8 personal notes, all 4
  agent drafts passed the checks, 74 outbox messages, chain verified.
- Web app (`app/web.py`, FastAPI + Jinja2 templates in `app/templates`, CSS in
  `app/static/style.css`, no JS framework). Pages:
  - `/manager`: location picker, "Check now", recent evaluations, alerts to the manager;
  - `/e/{id}`: the decision page. Posture card, agent rationale and verified quotes, drivers
    with sources, hourly strip, what-would-change, approve/override, notification package
    and "Approve and send", Q&A, inputs and raw responses, ledger timeline, replay;
  - `/outbox`: simulated messages and manager alerts;
  - `/history`: reviewer table, with verify-chain;
  - `/raw/{fetch_id}` and `/healthz`.
  Every page needs sign-in (see Accounts below); reviewers see no action forms.
- `app/service.py`: shared steps. "Check now" records the engine result in about 5 s; the
  agent and the package drafting run in background threads. The page polls
  `/e/{id}/status` every 3 s and reloads once when the work finishes, reopening the sections
  that were open (a meta refresh would not reload when the URL has a #fragment).
- Monitor: `MONITOR_MINUTES` > 0 starts a background loop. It alerts the manager (a simulated
  SMS, recorded as a `manager_alert` event) only when a non-normal posture first appears or
  changes, or the deadline is within an hour with no decision. Its memory is keyed by pin
  and day, because all pins share one facility id, and is in memory (reset on restart).
  `MONITOR_DAY=tomorrow` watches the next calendar day, for the Sunday demo.
- Run locally: `python scripts/serve.py --db local` (`.claude/launch.json` has this as
  `closure-call-dev`).
- `/locations` (the "Change location" button on the manager page) re-pins the facility
  anywhere in the US four ways:
  - live hotspots ("Where the weather is now"): `service.live_hotspots()` wraps
    `hotspots.find(per_hazard=5)`, cached 10 min, about 1 s to build;
  - city search through OpenStreetMap Nominatim (`app/sources/geocode.py`, US only);
  - a Leaflet 1.9.4 map with SRI hashes and OSM tiles; tap to pin, or tap a hotspot dot;
  `POST /pin` calls `repin_to_point`, so NWS supplies the town and time zone, and the
  nearest gauge with flood stages is used. On startup the app restores the location of the
  most recent evaluation, so the pin survives restarts and redeploys. Use the live
  hotspots for demos, because storms pass.
- `scripts/reset_record.py [--db local] --yes-erase-everything` erases a whole decision
  record. It drops and recreates the tables, because the triggers block DELETE and TRUNCATE.
  Without the flag it only shows counts. Both records (local and Akamai) were erased on
  Oct 10 before the demo, at the user's request.
- Browser test Oct 10 (phone width): Montana for Sunday gave a closure. The agent finished in
  about 37 s and said Augusta "is not named; unclear", so the prompt fix holds. Approve, then
  package (about 26 s, all agent drafts passed), then send: 72 messages. Chain verified.
- Deployment: `Dockerfile` (python:3.12-slim, non-root, a single uvicorn process on purpose)
  and `docker-compose.yml`. Caddy (caddy:2.8) serves HTTPS on 443 with an automatic Let's
  Encrypt certificate for $DOMAIN and redirects port 80. The app listens only on the
  internal network (port 8000). Certificates persist in the `caddy_data` volume.
- `scripts/deploy.sh root@IP [domain]`. The domain defaults to `<ip-with-dashes>.sslip.io`
  (free wildcard DNS; a team-owned domain with an A record looks better). The script
  installs Docker from Ubuntu's packages (docker.io, docker-compose-v2) if missing and ships
  `git archive HEAD` to /opt/closure-call, so only committed code goes and .env is never in
  the image or archive. It copies .env by scp (chmod 600) and appends DOMAIN, BASE_URL
  (https), MONITOR_MINUTES (default 15) and MONITOR_DAY (default tomorrow). Then it runs
  `docker compose up -d --build` and polls https://DOMAIN/healthz. Commit before deploying.
  The firewall must allow TCP 22, 80 and 443. Enable Linode disk encryption if the region
  offers it.
- LIVE (Oct 10): https://closure-call.algebramaniacabc.org on Linode 172.233.211.226 (Ubuntu
  24.04, 1 CPU, 2 GB). Code is in /opt/closure-call, run with `docker compose`. The Let's
  Encrypt cert is valid to Jan 8, 2027. The DNS A record is in Cloudflare and must stay
  "DNS only" (grey cloud): proxied mode breaks Caddy's certificate and redirect. The
  Linode's IP is on the Postgres allow list. The monitor runs every 15 min and watches
  tomorrow. Redeploy with `scripts/deploy.sh root@172.233.211.226
  closure-call.algebramaniacabc.org` after committing. Logs: `ssh root@172.233.211.226 'cd
  /opt/closure-call && docker compose logs app --tail 50'`.
- The agent once claimed the Augusta facility was "in the Lincoln valley". The prompts now
  forbid assuming a facility is inside a named area. Watch for this kind of geographic
  inference in demos.
- Accounts (`app/auth.py`): a `users` table in the same database but separate metadata, so
  `reset_record.py` never deletes accounts. Passwords use stdlib scrypt with a per-user salt.
  Sessions are a signed HttpOnly SameSite=Lax cookie (HMAC with SESSION_SECRET from .env,
  7 days), and carry a fingerprint of the password hash, so changing a password ends sessions.
  Roles: manager (check, re-pin, decide, draft, send), reviewer (read, verify, replay,
  ask), and sysadmin (the /admin page: add, disable and reset accounts, and erase history;
  cannot decide, for separation of duties). Everyone can change their own password at
  /account. Erasing (`Store.erase_all`, also used by `scripts/reset_record.py`) needs the
  word ERASE, a reason and the sysadmin's password. The new chain starts with a
  `record_erased` entry (who, why, counts, and the old chain's head hash), shown on /history.
- Admin log (`app/audit.py`): a separate append-only, hash-chained `admin_log` table with its
  own trigger function (`closure_call_admin_log_append_only`), so `erase_all` never drops it.
  It records every account change (added, disabled, enabled, password changed; never
  passwords) and every erasure. Each erasure's `record_erased` entry links to its admin log
  entry (`admin_log_seq`, `admin_log_hash`), so earlier erasures survive later ones. After each
  append, `AUDIT ANCHOR seq=... hash=...` goes to the server log as an anchor. Docker keeps
  only 3 x 10 MB of logs, so copy anchors elsewhere if they must last. The Admin page lists
  and verifies it; /history shows every erasure from it. A Postgres smoke test in a
  throwaway database passed (two erasures kept, UPDATE/DELETE/TRUNCATE blocked). Every ledger actor is `Display Name (@username, role)`. In-memory limits: 5 failed
  sign-ins per username or IP locks for 5 minutes; 8 checks or questions per user per 10
  minutes. Manage accounts with `python scripts/users.py add|list|passwd|disable|enable
  [--db local]`. It prompts for passwords; use PowerShell, since Git Bash echoes them. Notices
  to employees are still signed by the roster's facility manager (E001). `tests/test_auth.py`
  (13 tests).
- `.env.example`, `requirements.txt`, `.gitignore`, `README.md`.
- `.env` holds the team key and is gitignored. Never commit it or print the key.

## Open questions

- Whether USDA judges accept simulated notification sending, and which deliverable they weigh most.
- Whether AWS judges mind a non-AWS model provider behind Strands (we use no AWS cloud services).
- There is no git remote yet: push to GitHub before the 8 AM submission.
- Per-facility thresholds within global bounds: discussed and deferred until after the hackathon.

## Operations notes (Oct 11)

- `constraints.txt` pins every package to what ran in the live container; the Dockerfile installs
  `requirements.txt` under it, so redeploys cannot pull an untested release.
- `MODEL_ACTIONS_PER_10_MIN` (default 20) caps checks and questions per user.
- The river gauge is optional: an NWPS outage adds a note and the check continues on alerts alone.
- The monitor's deadline reminder counts a decision on any evaluation for that day.
- Browser push (Oct 11, `app/push.py`): Web Push with VAPID keys from `.env` (VAPID_PUBLIC_KEY,
  VAPID_PRIVATE_KEY, VAPID_SUBJECT; generated without being displayed). Devices subscribe on
  /account (managers only), stored in `push_subscriptions`; 404/410 from a push service removes
  the device. The monitor sends a real push to every active manager device only when the
  recommendation ESCALATES (`service.severity`: open < leave option < delay or early dismissal <
  closure; a longer delay is worse). Downgrades and deadline reminders stay in the app. Each
  manager_alert records `push: {sent, failed, expired_removed}`. `/sw.js` is served from the
  root (public, Service-Worker-Allowed: /). The manifest and icons in app/static make the site
  installable, which iPhone requires for web push (Add to Home Screen, iOS 16.4+).
  pywebpush 2.5.0 and its dependencies are pinned in constraints.txt.
- The agent no longer writes a headline sentence (Oct 11): the posture card is the headline. "Why"
  opens straight on the reason count and rows (plain code, instant); the model's parts (email
  paragraph, caveats, quotes, tested what-ifs) fill in below when it finishes. `AgentReport.headline`
  stays as an optional field so older records still load.
- Tropical is a fourth hazard category (Oct 11): Hurricane Warning and Extreme Wind Warning
  (moved out of wind), Tropical Storm Warning, and Storm Surge Warning are warning level and
  extended by road recovery. Hurricane, Tropical Storm and Storm Surge Watches are watches that
  upgrade to their warnings. Sources are the NHC/NWS product definitions in thresholds.json.
  Reason rows carry a hazard tag (flood/wind/winter/tropical); hotspots and the map have a
  tropical group; storm surge triggers the flood safety line. On Oct 11 no tropical
  alerts were active in the US, so this is tested only with synthetic alerts.
- AI only after the decision (Oct 11, user's call): "Check now" and the monitor no longer run the
  investigation agent; the explanation paragraph is the rules engine's; caveats are gone. "Does
  this alert apply here?" is answered in code (`web.applies_here`): the alert was returned for the
  exact coordinates, elevation limits in the text vs the facility elevation, and the forecast at
  the point. After a decision, one AI request ("Write summary and draft notices") writes a decision
  summary for leadership plus each group's notices (`notify.draft_groups`). Code checks the summary
  names the decider followed by approved/overrode, has the day and key facts, and uses no gendered
  pronouns; otherwise `summary_template` is used. Questions to the AI open only after a decision.
  `app/agent.py`'s investigation remains in the code but is no longer called by the app.
- Always use the Write/Edit tools for files and patch scripts, not bash heredocs.
- Staff and facility editing (Oct 11, `app/organization.py`, page /organization, nav "Staff"): managers and
  sysadmins edit the facility profile (name, agencies, facility manager, hours, operating days, commute
  windows, decide-by time, holidays) and the roster (add, edit, remove; leave as date pairs, detail as office and
  dates, accessibility as checkboxes with notes; no JSON for managers). Reviewers read only. Saved copies live in the `org_config` table (own metadata, so erasing keeps
  them) and override data/*.json through `facility.set_override`; `load_facility()`/`load_roster()` callers
  are unchanged. Each save is parsed as the app loads it before being kept, and goes in the admin log
  (employee_added/changed/removed, facility_changed). The facility manager cannot be removed. The location
  stays on /locations. `tests/test_org.py`.
