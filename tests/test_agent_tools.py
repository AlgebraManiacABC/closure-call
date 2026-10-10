"""Agent tool checks that need no model: quote verification and engine re-runs.

Run from the repo root:  python tests/test_agent_tools.py
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.agent import Investigation  # noqa: E402
from app.rules import AlertIn, evaluate  # noqa: E402
from test_rules import NOW, TZ, make  # noqa: E402

TEXT = ("* WHAT...Heavy snow. Total snow accumulations between 10 and 20 inches.\n"
        "* WHEN...Until 6 AM MDT Monday.")


def setup():
    a = AlertIn(id="urn:x", event="Winter Storm Warning", hazard="winter", severity="Severe", certainty="Likely",
                urgency="Expected", starts=datetime(2026, 10, 12, 18, tzinfo=TZ), ends=None, headline="h",
                sender="NWS", description=TEXT)
    inputs = make(alerts=[a])
    inv = Investigation(inputs, evaluate(inputs))
    return inv, {t.tool_name: t for t in inv.tools(network=False)}


def test_verbatim_quote_is_recorded_despite_spacing_and_dashes():
    inv, tools = setup()
    out = tools["record_evidence"](alert_id="a1", quote="Total snow   accumulations between 10 and 20 inches.",
                                   kind="snow_total", value=20, unit="in")
    assert out == "recorded" and inv.evidence[0].alert_event == "Winter Storm Warning"


def test_paraphrased_quote_is_rejected():
    inv, tools = setup()
    out = tools["record_evidence"](alert_id="A1", quote="Snow totals of 10 to 20 inches.", kind="snow_total",
                                   value=20, unit="in")
    assert out.startswith("REJECTED") and not inv.evidence and inv.rejected


def test_what_if_reruns_engine():
    inv, tools = setup()
    assert inv.rec.posture == "closure"
    out = tools["what_if"](change="remove_alert", alert_id="A1")
    assert "Open" in out and inv.what_ifs[0].posture == "normal"


if __name__ == "__main__":
    for k, v in dict(globals()).items():
        if k.startswith("test_"):
            v()
            print("ok", k)
