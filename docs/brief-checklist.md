# USDA "Closure Call" brief: where each requirement is met

Checked against `USDA.md` on Oct 11, 2026. Paths are pages on the live site (sign-in required) or files
in this repo.

## Deliverables

| # | The brief asks for | Where it is |
|---|---|---|
| 1 | A working system with a live demo on real NWS data, not a mock feed | Every check calls the live NWS API (alerts, hourly gridpoint forecast, observations), NOAA NWPS river gauges and USGS. **Change location** lists places with active NWS warnings right now, so there is always real weather to demo. |
| 2 | Recommended posture (normal / delay / early dismissal / telework / closure) | Decision page, top card: "Recommendation: ..." with OPM's wording underneath. |
| 2 | Every input that drove it, with source and timestamp | Each reason row opens to its source, issue time and fetch time. "Sources and raw data" lists every source with times, plus every raw API response with its SHA-256. |
| 2 | Which threshold was crossed and by how much | Each reason row ("so 0.9 ft over"), and "All threshold checks" (worst forecast vs cutoff, crossed by or how far away). |
| 2 | Confidence, and what would change the recommendation | The **Confidence** chip (tap for reasons). **What would change it**: the exact change that crosses each threshold, verified by re-running the rules, plus the agent's tested scenarios. |
| 2 | A one-paragraph plain-language rationale a manager could paste into an email | **Plain-language explanation** with a **Copy paragraph** button, written by the rules engine from the recorded data, so it is there instantly. After the decision, AI writes a **decision summary** (who decided, approve or override, the reason, who is notified) with its own copy button; code checks it names the decider and the action. |
| 2 | (Does an alert apply to this facility?) | Each alert reason answers it in code: NWS returned the alert for the facility's exact coordinates; elevation limits in the alert text ("above 6000 feet") are compared with the facility's elevation; and the forecast at the facility itself is shown for that hazard. |
| 3 | The recipient list, with the logic for who is included and who is not | After a decision: **Who is notified and why** and **Who is skipped and why**, one reason per person (on detail elsewhere, on leave, teleworking under an early dismissal, essential staff, detailed in from another office). |
| 3 | A drafted message | One text and one email per employee group, checked in code for the day, the delay or time, and "closed"; personal notes (early shift, flood-prone commute, accessibility needs) are added per person. |
| 3 | Channel routing | Text and email for closures, delays and early dismissals; each person's preferred channel for the unscheduled-leave option. Deaf employees are never called. Send-by time shown. |
| 4 | The decision record: what was known, when, and what was decided, reconstructable later | **History**: append-only, hash-chained ledger of every evaluation (inputs, raw responses, rules version), agent report, question, decision with reason, notification package and send. **Verify the whole record** recomputes every hash; **Replay** re-runs the rules on recorded inputs and must match. Erasures and account changes go to a separate admin log. |
| 5 | Threshold defensibility: where the cutoffs came from | **Thresholds and sources** (footer link, `/thresholds`): every cutoff with its NWS directive or OPM source; our own assumptions are labelled with what would replace them. Source file: `data/thresholds.json`. |

## Scope

| The brief says | How we met it |
|---|---|
| NWS / NOAA as the core source | NWS alerts, gridpoint forecast and observations. |
| A synthetic facility model: location, hours, assignments, essential flags, telework eligibility, accessibility needs | `data/facility.json` and `data/roster.json` (40 people, including details in and out, leave, telework, essential staff and four accessibility needs). |
| At least one supplementary signal | River gauges: NOAA NWPS (official flood stages, crest forecast, NWS impact statements naming affected roads) and USGS readings as a cross-check. |
| A graded recommendation, not a binary | Five postures, chosen by timing as well as severity (a morning hazard that clears gives a delay; one arriving mid-day gives an early dismissal). |
| Right people, right channel, right time, right content | See deliverable 3; delays and closures must be announced by 5 AM local. |
| One facility profile and two or three hazards, done well | One USDA Service Center; flooding, high wind and winter weather, plus hurricanes and tropical storms as their own category (they arrive as wind and storm surge together). |
| The system recommends; a person decides | A manager approves or overrides (with a written reason) before anything is sent; sending is simulated. |

## Known limits to mention if asked

- Sending is simulated: the outbox shows what would be sent.
- The facility is one synthetic Service Center that can be moved to wherever weather is active.
- Two values are our own assumptions (roads recover 3 hours after snow or ice ends; flooded roads stay
  closed 12 hours after the river falls) and are labelled as such.
- NWS criteria vary by local office; the national values used here are documented with that caveat.
