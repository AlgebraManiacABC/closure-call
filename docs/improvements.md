# Room for improvement

Found in a full review on Oct 11, 2026. Ordered by priority within each group. Items marked
**before submission** matter for the 8 AM deadline or the pitch; the rest are for afterwards.

## Demo and submission

- **Push the repo to GitHub (before submission).** There is no git remote, so the code exists only on
  the laptop, and most submission forms ask for a repository link.
- **Pre-run the demo evaluation (before submission).** Around the 7 PM checkpoint the shared inference
  endpoint slowed from about 4 s to 30 s per call; one investigation took 4.5 minutes. The rules
  engine's recommendation appears in seconds either way, but the agent's rationale may not.
- **Questions are answered inside the web request.** On a slow endpoint the page can sit on
  "Thinking..." for minutes. Run answers in the background like the investigation.
- **The monitor records an evaluation every 15 minutes.** That is good for "what was known when", but it
  fills "Recent evaluations" and History with automatic runs. Filter or collapse monitor runs, or
  record them only when something changed.
- **History and recent lists have no location column.** Every pin shares one facility id, so runs at
  different places look alike. Show the town.

## Security

- **A sysadmin can reset a manager's password and then sign in as that manager.** The admin log records
  the reset, but nothing prevents it, which weakens the separation of duties. Make admin-set passwords
  temporary (forced change at next sign-in) and tell the user their password was reset.
- **No CSRF tokens.** Actions rely on POST plus SameSite=Lax cookies, which modern browsers enforce.
  Add tokens for defense in depth.
- **Rate limits and sign-in lockouts live in memory**, so a restart resets them, and they assume one
  process.
- **Audit anchors live only in Docker's logs** (about 30 MB, rotated). Ship them somewhere a database
  admin cannot reach, such as a separate log service or a daily email.
- **Unverified infrastructure settings:** confirm the Linode Cloud Firewall allows only 22, 80 and 443,
  that disk encryption is on, and what backups the database plan includes.
- **Sessions last 7 days** and can only be ended by a password change or by disabling the account.

## Correctness and trust

- **The model's free text is not machine-checked.** Quotes are verified, but the headline, rationale and
  answers can still contain wrong inferences (it once placed a facility in the wrong valley, and once
  named the wrong county). Add a numbers check: every figure in an answer must appear in the recorded
  data, or the answer gets an "unverified" badge.
- **Check the threshold citations (before submission).** The NWS directive numbers and OPM page paths in
  `data/thresholds.json` were written from memory.
- **Replace the assumptions with sources.** Road recovery (3 h after snow or ice) and flood recession
  (12 h) are our own; state DOT level-of-service targets and county road practice would replace them.
- **The flood gauge is the nearest one with flood stages within 25 miles.** It may be on a different
  river from the facility's access roads. Let each facility name its gauge and the stage at which its
  access road floods.
- **Hotspot coordinates are polygon centroids**, which can fall outside a concave alert area. Confirm
  each hotspot with an alerts-at-point lookup before listing it.
- **Engine explanations use 24-hour times** ("until 18:00"). Changing the wording changes replays of
  recorded decisions, so do it with a new rules version, not in place.
- **Failed answers are recorded as answers** ("The assistant is unavailable right now"). Record the
  failure as such, or not at all.
- **The monitor's memory resets on restart**, so it may re-alert once after a redeploy.

## Product

- **Per-facility thresholds within global bounds**, with a regional layer that follows the local NWS
  office's own criteria. Discussed on Oct 10; needs real facility records first.
- **Real sending**: SMS and email providers, delivery receipts, and replies.
- **Real facilities**: a facilities table with its own location, roster and gauge, replacing the single
  re-pinnable demo facility. The roster's detail-office names are Iowa places.
- **A threshold write-up for judges** (before submission, if time allows): one page from
  `data/thresholds.json` listing every cutoff, its source, and which values are assumptions.

## Engineering

- **No CI.** The tests are plain scripts (pytest-compatible). Run them on every push.
- **Python differs**: 3.11 in the local venv, 3.12 in the container.
- **The base image tag floats** (`python:3.12-slim`). Pin it by digest, as `constraints.txt` now pins
  the packages.
- **No schema migrations.** Tables are created with `create_all`; adding a column to a live table will
  need a migration tool such as Alembic.
- **`@app.on_event("startup")` is deprecated** in FastAPI; move the monitor start to a lifespan handler.
