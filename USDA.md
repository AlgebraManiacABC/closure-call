# Closure Call: Weather-Driven Facility Decisions and Notification
### The Problem Question
When severe weather threatens a federal facility, how does the decision to close — and the notification of every employee assigned to that building — happen fast enough, consistently enough, and with a clear enough record of why?
Build a system that ingests National Weather Service data alongside other operational signals, evaluates threat against a facility, recommends a closure posture to a human decision-maker, and generates the notification that reaches the right employees.

### Background

USDA has offices in nearly every county in the country — Service Centers, research stations, labs, field offices, and headquarters buildings, spread across every climate zone in the US and its territories. Ice storms in Iowa, hurricanes on the Gulf, wildfire smoke in the Mountain West, flooding in the Delta, extreme heat in the Southwest.

The decision to close a facility, delay opening, or shift to unscheduled telework is made by people, and it should be. But the inputs to that decision are scattered. A facility manager checks a forecast, maybe a state DOT road condition map, maybe a local school district's announcement, and makes a call — often at 4am, often under time pressure, often without a clear picture of who's actually affected.

Then the notification goes out, and that's its own problem. Who is assigned to this building? Who's on detail somewhere else this week? Who's already teleworking and doesn't need to be woken up? Which of them are essential personnel whose reporting requirements don't change? The employee roster and the building roster are frequently not the same list, and the message often arrives too late to change anyone's morning.

The consistency problem compounds it. Two facility managers facing identical conditions can reach different decisions, and neither can easily explain the reasoning afterward. When it goes wrong — a closure that should have happened and didn't — nobody can reconstruct what information was available at the time.

The goal is not to automate the decision. It's to make sure the person making it has everything they need in one place, in time, and that whatever they decide reaches the right people fast.

### Scope

**In scope: the data.**
1. National Weather Service / NOAA — the core source. The NWS API is public, free, requires no key, and provides active alerts, forecasts, and observations by point or zone. Alerts carry structured severity, certainty, and urgency fields that map naturally to thresholds.

2. A facility model — synthetic. Location, hours, employee assignments, essential-personnel flags, telework eligibility, accessibility needs.

3. At least one supplementary signal. This is where teams differentiate. State DOT road conditions and closures. School district and local government closure announcements. Air quality data during wildfire season. Power outage reporting. River gauge and flood-stage data. Anything that a real facility manager would actually check.



**In scope: the decision logic.**
The system should evaluate conditions against a facility and produce a graded recommendation, not a binary. Real closure postures include: normal operations, delayed arrival, early dismissal, unscheduled leave or telework, and full closure. Different thresholds, different notification urgency.


**In scope:**
the notification. Right people, right channel, right time, right content. An employee needs to know what's happening, whether it applies to them specifically, and what they're supposed to do — before they've already left the house.


**Out of scope.**
Don't build for every facility type in every hazard. One facility profile and two or three hazard types, handled well, beats broad and shallow.

### Deliverables
1. Working system. Live demo pulling real NWS data. Not a mock feed — the actual API, hitting real alerts somewhere in the country. There is almost always active severe weather somewhere in the US at any given hour; use it.
2. Recommendation with reasoning. For any facility and moment, the system outputs:
Recommended posture (normal / delay / early dismissal / telework / closure) Every input that drove it, with source and timestamp Which threshold was crossed and by how much Confidence, and what would change the recommendation A one-paragraph plain-language rationale a manager could paste into an email
3. Notification package. For a given recommendation: the recipient list with the logic behind who's included and who isn't, a drafted message, and channel routing.
4. The decision record. What was known, when, and what was decided. Reconstructable after the fact. This is the deliverable most teams will skip and the one an agency would care most about.
5. Threshold defensibility. Show your work on where the cutoffs came from. "We chose 25mph sustained winds" needs a reason. NWS alert severity fields, state DOT thresholds, and OPM operating-status categories are all legitimate anchors.

## USDA PRIZES
1. Letters of recommendation from the CTO & CDO of USDA
2. Recognition in USDA's AI Initatives
3. Networking Opportunities with USDA leadership
4. USDA Swag Package