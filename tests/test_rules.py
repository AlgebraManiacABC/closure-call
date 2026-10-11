"""Rules engine checks on synthetic inputs (no network).

Run from the repo root:  python -m pytest tests   (or just: python tests/test_rules.py)
"""
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rules import AlertIn, GaugeIn, HourIn, Inputs, SourceRef, evaluate  # noqa: E402

TZ = ZoneInfo("America/Chicago")
DAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 12, 20, 0, tzinfo=TZ)  # evening before


def make(hourly=None, alerts=(), gauge=None, now=NOW) -> Inputs:
    """hourly: {hour_of_DAY: {field: value}}; every other hour is calm."""
    start = datetime.combine(DAY - timedelta(days=1), time(12), tzinfo=TZ)
    hours = []
    for k in range(33):
        t = start + timedelta(hours=k)
        base = {"gust_mph": 10.0, "sustained_mph": 5.0, "snow_in": 0.0, "ice_in": 0.0, "wind_chill_f": 40.0}
        if t.date() == DAY and hourly and t.hour in hourly:
            base.update(hourly[t.hour])
        hours.append(HourIn(start=t, **base))
    ref = SourceRef(source="test", label="test", url="test", fetched_at=now)
    return Inputs(facility_id="f", facility_label="Test", lat=0, lon=0, time_zone="America/Chicago", day=DAY,
                  evaluated_at=now, open=time(8), close=time(16, 30), morning_commute=(time(6, 30), time(8, 30)),
                  evening_commute=(time(16), time(18)), decide_by=time(5), hours=hours, alerts=list(alerts),
                  gauge=gauge, sources={"grid": ref, "alerts": ref, "gauge": ref})


def gusts(hours, mph):
    return {h: {"gust_mph": mph} for h in hours}


def test_calm_is_normal():
    r = evaluate(make())
    assert r.posture == "normal" and r.confidence == "high"


def test_advisory_wind_gives_unscheduled_leave():
    r = evaluate(make(gusts(range(9, 12), 50)))
    assert r.posture == "unscheduled_leave"
    assert r.decisive[0].rule_id == "WIND-GUST-ADV" and r.decisive[0].margin == 4


def test_morning_wind_that_clears_gives_delay():
    r = evaluate(make(gusts([6, 7], 62)))  # hazardous 06:00-08:00
    assert r.posture == "delayed_arrival" and r.delay_hours == 2
    assert r.unscheduled_leave_option


def test_morning_wind_that_lingers_gives_longer_delay_then_closure():
    assert evaluate(make(gusts([6, 7, 8, 9], 62))).delay_hours == 4  # clears 10:00 -> commute 10:30
    assert evaluate(make(gusts(range(6, 12), 62))).posture == "closure"


def test_afternoon_wind_gives_early_dismissal():
    r = evaluate(make(gusts([15, 16, 17], 62)))
    assert r.posture == "early_dismissal" and r.dismissal_time.hour == 13


def test_late_morning_hazard_closes_instead_of_dismissing_before_noon():
    r = evaluate(make(gusts([11, 12], 62)))  # dismissal would be 09:00
    assert r.posture == "closure" and r.timing_rule == "TIME-CLOSE-EARLY-HAZARD"


def test_overnight_snow_counts_until_roads_recover():
    snow = {h: {"snow_in": 1.5} for h in range(0, 5)}  # 7.5 in, ends 05:00
    r = evaluate(make(snow))
    assert r.posture == "delayed_arrival"  # hazardous through 07:00 (3 h recovery), clears by 08:30
    assert any(f.rule_id == "SNOW-WARN" for f in r.decisive)


def test_winter_storm_warning_all_day_closes_with_reduced_confidence():
    a = AlertIn(id="a1", event="Winter Storm Warning", hazard="winter", severity="Severe", certainty="Likely",
                urgency="Expected", starts=datetime(2026, 10, 12, 18, tzinfo=TZ), ends=None, headline="", sender="")
    r = evaluate(make(alerts=[a]))
    assert r.posture == "closure" and r.confidence == "medium"  # alert alone, grid disagrees
    assert any("cancelled" in c.what for c in r.what_would_change)


def test_flood_gauge_near_major_reports_exact_tipping_point():
    g = GaugeIn(lid="X", name="X", distance_mi=1, stages_ft={"moderate": 11.5, "major": 12.5}, impacts=[],
                latest_stage_ft=12.4, latest_time=None, crest_stage_ft=None, crest_time=None, forecast_issued=None)
    inp = make(gauge=g)
    inp = inp.model_copy(update={"hours": [h.model_copy(update={"gauge_stage_ft": 12.4}) for h in inp.hours]})
    r = evaluate(inp)
    assert r.posture == "unscheduled_leave" and r.confidence == "medium"  # 0.1 ft short of major
    assert any(c.posture == "closure" and "+0.11" in c.what for c in r.what_would_change)


def _alert(event, certainty="Likely"):
    from app.sources.nws import EVENT_HAZARD
    return AlertIn(id=event, event=event, hazard=EVENT_HAZARD[event], severity="Extreme", certainty=certainty,
                   urgency="Expected", starts=datetime(2026, 10, 12, 18, tzinfo=TZ), ends=None, headline="", sender="")


def test_hurricane_warning_is_its_own_tropical_category():
    r = evaluate(make(alerts=[_alert("Hurricane Warning")]))
    assert r.posture == "closure"
    assert r.decisive[0].hazard == "tropical" and r.decisive[0].rule_id == "TROPICAL-HURRICANE-WARN"
    assert r.hazard_peak_levels["tropical"] == 2 and r.hazard_peak_levels["wind"] == 0  # not swept into wind
    surge = evaluate(make(alerts=[_alert("Storm Surge Warning")]))
    assert surge.posture == "closure" and surge.decisive[0].rule_id == "TROPICAL-SURGE-WARN"


def test_hurricane_watch_is_a_watch_that_would_close_if_upgraded():
    r = evaluate(make(alerts=[_alert("Hurricane Watch", certainty="Possible")]))
    assert r.posture == "normal" and r.confidence == "medium" and r.watches[0].event == "Hurricane Watch"
    assert any("upgraded to Hurricane Warning" in c.what and c.posture == "closure" for c in r.what_would_change)


def test_identical_inputs_give_identical_output():
    inp = make(gusts([15, 16], 60))
    again = Inputs.model_validate_json(inp.model_dump_json())
    assert evaluate(inp).model_dump_json() == evaluate(again).model_dump_json()


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
