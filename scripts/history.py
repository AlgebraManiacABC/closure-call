"""Reads the decision record: history, one evaluation's full timeline, chain verification, replay.

Run from the repo root:
    python scripts/history.py                 # recent evaluations and their decisions
    python scripts/history.py --show <id>     # everything known and decided for one evaluation
    python scripts/history.py --verify        # recompute the hash chain
    python scripts/history.py --replay <id>   # re-run the rules on the recorded inputs and compare
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.record import get_store  # noqa: E402


def describe(e: dict) -> str:
    p = e["payload"]
    if e["kind"] == "evaluation":
        return f"recommended {p['recommendation']['posture_label']}"
    if e["kind"] == "agent_report":
        r = p["report"]
        return f"agent {'ran' if r['ran'] else 'did not run'}: {len(r['evidence'])} verified quotes, {r['reason']}"
    if e["kind"] == "question":
        return f"Q: {p['question']}  A: {p['answer'][:140]}"
    if e["kind"] == "decision":
        return f"{p['action'].upper()}: {p['posture_label']}" + (f"  reason: {p['reason']}" if p["reason"] else "")
    return json.dumps(p)[:140]


ap = argparse.ArgumentParser()
ap.add_argument("--show")
ap.add_argument("--verify", action="store_true")
ap.add_argument("--replay")
args = ap.parse_args()
store = get_store()
print(f"Decision record: {store.url_safe}\n")

if args.verify:
    v = store.verify_chain()
    print(f"Chain {'OK' if v['ok'] else 'BROKEN'}: {v['entries']} entries, head {v['head'][:16]}")
    for p in v["problems"]:
        print("  -", p)
elif args.replay:
    r = store.replay(args.replay)
    verdict = "IDENTICAL" if r["matches"] else "DIFFERS in " + ", ".join(r["differences"])
    print(f"Replay with rules {r['rules_version']}: recorded {r['recorded_posture']}, "
          f"replayed {r['replayed_posture']} -> {verdict}")
elif args.show:
    d = store.load(args.show)
    i, rec = d["inputs"], d["recommendation"]
    print(f"{i.facility_label} ({i.lat}, {i.lon}), decision day {i.day}, evaluated {i.evaluated_at}")
    print(f"Recommended: {rec.posture_label} [confidence {rec.confidence}, rules {rec.rules_version}]")
    print("\nWhat was known (sources):")
    for k, s in i.sources.items():
        print(f"  {k:7} {s.label}; issued {s.issued or '-'}; fetched {s.fetched_at:%Y-%m-%d %H:%M:%SZ}")
    raws = d["evaluation"]["payload"]["raw_fetches"]
    print(f"  {len(raws)} raw responses stored ({sum(r['size'] for r in raws):,} bytes), each with its sha256")
    print("\nTimeline:")
    for e in d["events"]:
        print(f"  #{e['seq']:<4} {e['at']}  {e['kind']:12} by {e['actor']:28} {describe(e)}")
        print(f"        hash {e['hash'][:16]} <- prev {e['prev_hash'][:16]}")
else:
    rows = store.history()
    if not rows:
        print("No evaluations recorded yet.")
    for h in rows:
        dec = h["decision"]
        status = f"{dec['action']} by {dec['by']}: {dec['label']}" if dec else "AWAITING DECISION"
        print(f"{h['at']}  {h['facility_id']}  day {h['day']}  recommended {h['recommended']:17} -> {status}")
        print(f"    id {h['evaluation_id']}  agent={'yes' if h['agent'] else 'no'}  questions={h['questions']}")
