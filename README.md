# Safety Call

Weather-driven closure decisions and employee notification for a federal facility.
Built solo at Hack Midwest 2026 for the USDA "Closure Call" challenge, the AWS Strands
Agents challenge, and the Akamai Cloud AI challenge.

Live: https://closure-call.algebramaniacabc.org (sign-in required)

## What it does

When severe weather threatens a USDA Service Center, Safety Call:

1. **Pulls live data**: NWS alerts, the NWS hourly gridpoint forecast, NOAA river gauges with
   their official flood stages and impact statements, and USGS readings.
2. **Recommends a posture** with a deterministic rules engine: normal, unscheduled leave or
   telework, delayed arrival, early dismissal, or closure (OPM's operating-status categories).
   Every cutoff in `data/thresholds.json` sits next to its source; our own assumptions are marked.
3. **Investigates and explains** with a Strands agent on Akamai's inference endpoint. It quotes the
   NWS alert text (each quote verified word for word in code), runs follow-up lookups, tests what
   would change the posture by re-running the rules, and writes a plain-language rationale.
4. **Lets a person decide.** The facility manager approves or overrides, with a written reason.
5. **Notifies the right people.** Plain code sorts the roster (on detail, teleworking, on leave,
   essential, accessibility needs) into notify or skip with a reason for each person. The agent
   drafts one text and email per group, code checks the drafts, and sending is simulated.
6. **Keeps a decision record**: an append-only, hash-chained ledger of what was known, when, and
   what was decided, with every raw API response. Any decision can be replayed to show the rules
   give the same answer. A separate admin log records account changes and erasures.

The system recommends; a person decides. Plain code decides; the model investigates and explains.

How each requirement of the USDA brief is met: [docs/brief-checklist.md](docs/brief-checklist.md).

## Stack

- Python, FastAPI, server-rendered pages (phone-first)
- Strands Agents SDK 1.59 with an OpenAI-compatible model on Akamai's hosted inference endpoint
- Akamai Cloud Linode (Docker Compose, Caddy for HTTPS) and Akamai Managed PostgreSQL
- See the cloud write-up for how these were set up and managed

## Run it locally

    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    copy .env.example .env          (then fill in the keys; DATABASE_URL is optional)
    python scripts/users.py add yourname --name "Your Name" --role manager --db local
    python scripts/serve.py --db local

Then open http://localhost:8000. `--db local` uses a SQLite file instead of the live database.

## Tests

    python tests/test_rules.py        (rules engine, 12)
    python tests/test_agent_tools.py  (quote verification and what-ifs, 3)
    python tests/test_record.py       (decision record, triggers, tamper detection, 5)
    python tests/test_notify.py       (who is notified, what they are told, decision summary, 8)
    python tests/test_auth.py         (sign-in, roles, admin log, deliverables, push, 15)

None of them need the network or the model.

## Deploy

Commit, then `scripts/deploy.sh root@SERVER_IP your.domain`. It ships the committed code over SSH,
copies `.env` separately, builds with the pinned versions in `constraints.txt`, and waits for HTTPS.

## Roles

- **Manager**: checks conditions, moves the facility, decides, drafts and sends notices.
- **Reviewer**: reads everything, verifies the record, replays decisions, asks questions.
- **Sysadmin**: manages accounts and can erase history (which is logged); cannot decide.
