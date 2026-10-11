"""Decision record: an append-only, hash-chained ledger of what was known, when, and what was decided.

Tables (all append-only; database triggers reject UPDATE, DELETE and, on Postgres, TRUNCATE):
  ledger       one row per event: evaluation, agent_report, question, decision (later: notifications).
               `entry` holds the exact canonical JSON of the event; `hash` = sha256(prev_hash + entry),
               so altering or removing any row breaks the chain from that point (see verify_chain).
               The other columns are copies of entry fields, for querying.
  raw_fetches  every raw API response, gzipped, with the sha256 of its canonical JSON. The evaluation
               event lists these hashes, so the chain covers the raw data too.
  rule_sets    each thresholds.json version, keyed by the first 12 hex chars of its own sha256.

Works on SQLite (local default) and Postgres (Akamai Managed PostgreSQL) via DATABASE_URL.
"""
import gzip
import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from dotenv import load_dotenv

from .agent import AgentReport
from .audit import AuditLog
from .rules import THRESHOLDS_PATH, Inputs, Recommendation, evaluate, rules_version, thresholds_text
from .sources.http import Fetched

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)
GENESIS = "0" * 64
POSTURES = ("normal", "unscheduled_leave", "delayed_arrival", "early_dismissal", "closure")

meta = sa.MetaData()
SeqType = sa.BigInteger().with_variant(sa.Integer(), "sqlite")  # SQLite autoincrements INTEGER keys only

ledger = sa.Table(
    "ledger", meta,
    sa.Column("seq", SeqType, primary_key=True, autoincrement=True),
    sa.Column("id", sa.String(36), nullable=False, unique=True),
    sa.Column("at", sa.String(40), nullable=False),
    sa.Column("kind", sa.String(32), nullable=False, index=True),
    sa.Column("facility_id", sa.String(64), nullable=False, index=True),
    sa.Column("evaluation_id", sa.String(36), index=True),
    sa.Column("actor", sa.String(128), nullable=False),
    sa.Column("day", sa.String(10)),
    sa.Column("posture", sa.String(32)),
    sa.Column("entry", sa.Text, nullable=False),
    sa.Column("prev_hash", sa.String(64), nullable=False),
    sa.Column("hash", sa.String(64), nullable=False, unique=True),
)
raw_fetches = sa.Table(
    "raw_fetches", meta,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("evaluation_id", sa.String(36), nullable=False, index=True),
    sa.Column("source", sa.String(16), nullable=False),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("fetched_at", sa.String(40), nullable=False),
    sa.Column("status", sa.Integer, nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("size", sa.Integer, nullable=False),
    sa.Column("body_gz", sa.LargeBinary, nullable=False),
)
rule_sets = sa.Table(
    "rule_sets", meta,
    sa.Column("version", sa.String(12), primary_key=True),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
)
APPEND_ONLY = ("ledger", "raw_fetches", "rule_sets")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class EntryRef:
    id: str
    seq: int
    hash: str


class RecordError(ValueError):
    pass


def normalize_url(url: str) -> sa.URL:
    """Accepts Postgres URLs as Akamai Cloud Manager shows them (postgres://...), selects the psycopg
    driver, and requires TLS unless the URL says otherwise."""
    u = sa.make_url(url.replace("postgres://", "postgresql://", 1) if url.startswith("postgres://") else url)
    if u.drivername in ("postgresql", "postgresql+psycopg2"):
        u = u.set(drivername="postgresql+psycopg")
    if u.drivername.startswith("postgresql") and "sslmode" not in u.query:
        u = u.update_query_dict({"sslmode": "require"})
    return u


class Store:
    def __init__(self, url: str | None = None):
        url = url or os.environ.get("DATABASE_URL") or LOCAL_DB
        self.engine = sa.create_engine(normalize_url(url), pool_pre_ping=True)
        self.dialect = self.engine.dialect.name
        self._lock = threading.RLock()  # re-entrant: _append can run inside a caller's transaction
        meta.create_all(self.engine)
        self._install_triggers()
        self.audit = AuditLog(self.engine)  # account changes and erasures; never dropped by erase_all

    @property
    def url_safe(self) -> str:
        return self.engine.url.render_as_string(hide_password=True)

    def _install_triggers(self):
        with self.engine.begin() as c:
            if self.dialect == "postgresql":
                c.exec_driver_sql("""
                    CREATE OR REPLACE FUNCTION closure_call_append_only() RETURNS trigger AS $$
                    BEGIN
                      RAISE EXCEPTION USING MESSAGE =
                        'decision record is append-only: ' || TG_OP || ' on ' || TG_TABLE_NAME || ' is not allowed';
                    END $$ LANGUAGE plpgsql""")
                for t in APPEND_ONLY:
                    c.exec_driver_sql(f"DROP TRIGGER IF EXISTS {t}_append_only ON {t}")
                    c.exec_driver_sql(f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} "
                                      f"FOR EACH ROW EXECUTE FUNCTION closure_call_append_only()")
                    c.exec_driver_sql(f"DROP TRIGGER IF EXISTS {t}_no_truncate ON {t}")
                    c.exec_driver_sql(f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} "
                                      f"FOR EACH STATEMENT EXECUTE FUNCTION closure_call_append_only()")
            elif self.dialect == "sqlite":
                for t in APPEND_ONLY:
                    for op in ("UPDATE", "DELETE"):
                        c.exec_driver_sql(
                            f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                            f"BEGIN SELECT RAISE(ABORT, 'decision record is append-only: {op} on {t} is not allowed'); END")

    # ---------- writing ----------

    def _append(self, kind: str, facility_id: str, payload: dict, actor: str, evaluation_id: str | None = None,
                day: str | None = None, posture: str | None = None, entry_id: str | None = None,
                conn=None) -> EntryRef:
        header = {"id": entry_id or str(uuid.uuid4()), "at": now_iso(), "kind": kind, "facility_id": facility_id,
                  "evaluation_id": evaluation_id, "actor": actor, "day": day, "posture": posture}
        entry = canonical({**header, "payload": payload})

        def write(c):
            if self.dialect == "postgresql":
                c.exec_driver_sql("LOCK TABLE ledger IN SHARE ROW EXCLUSIVE MODE")
            last = c.execute(sa.select(ledger.c.hash).order_by(ledger.c.seq.desc()).limit(1)).scalar()
            prev = last or GENESIS
            h = sha256(prev + entry)
            seq = c.execute(ledger.insert().values(**header, entry=entry, prev_hash=prev, hash=h)).inserted_primary_key[0]
            return EntryRef(header["id"], seq, h)

        with self._lock:
            if conn is not None:
                return write(conn)
            with self.engine.begin() as c:
                return write(c)

    def _store_fetches(self, c, evaluation_id: str, fetched: list[Fetched]) -> list[dict]:
        metas = []
        for f in fetched:
            body = canonical(f.body).encode("utf-8")
            m = {"id": str(uuid.uuid4()), "source": f.source, "url": f.url,
                 "fetched_at": f.fetched_at.isoformat(timespec="seconds"), "status": f.status,
                 "sha256": sha256(body), "size": len(body)}
            c.execute(raw_fetches.insert().values(**m, evaluation_id=evaluation_id, body_gz=gzip.compress(body)))
            metas.append(m)
        return metas

    def _store_rule_set(self, c, version: str, content: str):
        exists = c.execute(sa.select(rule_sets.c.version).where(rule_sets.c.version == version)).first()
        if not exists:
            c.execute(rule_sets.insert().values(version=version, created_at=now_iso(), content=content))

    def record_evaluation(self, inputs: Inputs, rec: Recommendation, fetched: list[Fetched],
                          thresholds_path: Path = THRESHOLDS_PATH, actor: str = "system") -> EntryRef:
        content = thresholds_text(thresholds_path)
        version = rules_version(content)
        if rec.rules_version and rec.rules_version != version:
            raise RecordError("thresholds.json changed since this recommendation was computed; re-evaluate first")
        evaluation_id = str(uuid.uuid4())
        with self._lock, self.engine.begin() as c:
            self._store_rule_set(c, version, content)
            metas = self._store_fetches(c, evaluation_id, fetched)
            payload = {"rules_version": version, "raw_fetches": metas,
                       "inputs": inputs.model_dump(mode="json"), "recommendation": rec.model_dump(mode="json")}
            return self._append("evaluation", inputs.facility_id, payload, actor, evaluation_id=evaluation_id,
                                day=str(inputs.day), posture=rec.posture, entry_id=evaluation_id, conn=c)

    def record_agent_report(self, evaluation_id: str, report: AgentReport, fetched: list[Fetched]) -> EntryRef:
        ev = self._get_entry(evaluation_id, "evaluation")
        with self._lock, self.engine.begin() as c:
            metas = self._store_fetches(c, evaluation_id, fetched)
            return self._append("agent_report", ev["facility_id"], {"report": report.model_dump(mode="json"),
                                                                     "raw_fetches": metas},
                                f"agent:{report.model or 'none'}", evaluation_id=evaluation_id, day=ev["day"], conn=c)

    def record_question(self, evaluation_id: str, question: str, answer: str, asked_by: str) -> EntryRef:
        ev = self._get_entry(evaluation_id, "evaluation")
        return self._append("question", ev["facility_id"], {"question": question, "answer": answer},
                            asked_by, evaluation_id=evaluation_id, day=ev["day"])

    def record_decision(self, evaluation_id: str, action: str, decided_by: str, reason: str = "",
                        posture: str | None = None, delay_hours: int | None = None,
                        dismissal_time: str | None = None) -> EntryRef:
        """The manager's approve or override. Overrides need a posture and a written reason."""
        ev = self._get_entry(evaluation_id, "evaluation")
        rec = ev["payload"]["recommendation"]
        if not decided_by.strip():
            raise RecordError("decided_by is required")
        if action == "approve":
            posture, delay_hours = rec["posture"], rec.get("delay_hours")
            dismissal_time = rec["dismissal_time"][11:16] if rec.get("dismissal_time") else None
            label = rec["posture_label"]
        elif action == "override":
            if posture not in POSTURES:
                raise RecordError(f"posture must be one of {POSTURES}")
            rec_time = rec["dismissal_time"][11:16] if rec.get("dismissal_time") else None
            if posture == rec["posture"] and (
                    posture not in ("delayed_arrival", "early_dismissal")
                    or (posture == "delayed_arrival" and delay_hours == rec.get("delay_hours"))
                    or (posture == "early_dismissal" and dismissal_time == rec_time)):
                raise RecordError("that is the recommendation itself; approve it instead of overriding")
            if len(reason.strip()) < 10:
                raise RecordError("an override needs a written reason (at least 10 characters)")
            if posture == "delayed_arrival" and not delay_hours:
                raise RecordError("a delayed arrival needs delay_hours")
            if posture == "early_dismissal" and not dismissal_time:
                raise RecordError("an early dismissal needs dismissal_time (HH:MM)")
            labels = json.loads(self.rule_set(rec["rules_version"]))["postures"]
            label = labels[posture].format(hours=delay_hours, time=dismissal_time or "")
        else:
            raise RecordError("action must be 'approve' or 'override'")
        payload = {"action": action, "posture": posture, "posture_label": label, "delay_hours": delay_hours,
                   "dismissal_time": dismissal_time, "reason": reason.strip(),
                   "recommended_posture": rec["posture"], "recommended_label": rec["posture_label"],
                   "evaluation_hash": ev["hash"]}
        return self._append("decision", ev["facility_id"], payload, decided_by.strip(), evaluation_id=evaluation_id,
                            day=ev["day"], posture=posture)

    def latest_decision(self, evaluation_id: str) -> dict:
        decisions = [e for e in self.timeline(evaluation_id) if e["kind"] == "decision"]
        if not decisions:
            raise RecordError("no decision recorded yet; the manager must approve or override first")
        return decisions[-1]

    def record_notification_package(self, evaluation_id: str, package) -> EntryRef:
        """Drafted package (recipients with reasons, group messages). Not sent."""
        decision = self.latest_decision(evaluation_id)
        if package.decided.posture != decision["payload"]["posture"]:
            raise RecordError("package does not match the latest decision; rebuild it")
        return self._append("notification_package", decision["facility_id"],
                            {"decision_hash": decision["hash"], "package": package.model_dump(mode="json")},
                            "system", evaluation_id=evaluation_id, day=decision["day"], posture=package.decided.posture)

    def record_manager_alert(self, evaluation_id: str, reason: str, manager: str, phone: str, body: str,
                             push: dict | None = None) -> EntryRef:
        """The app prompting the facility manager because something changed. The text message is simulated;
        `push` records real browser-push delivery to managers' devices, when one was sent (escalations)."""
        ev = self._get_entry(evaluation_id, "evaluation")
        payload = {"reason": reason, "to": manager, "phone": phone, "channel": "sms", "body": body, "mode": "simulated"}
        if push is not None:
            payload["push"] = push
        return self._append("manager_alert", ev["facility_id"], payload, "monitor", evaluation_id=evaluation_id,
                            day=ev["day"], posture=ev["posture"])

    def outbox(self, limit: int = 200) -> list[dict]:
        """Everything the system has 'sent' (simulated): employee notices and manager alerts, newest first."""
        with self.engine.connect() as c:
            rows = c.execute(sa.select(ledger).where(ledger.c.kind.in_(["notifications_sent", "manager_alert"]))
                             .order_by(ledger.c.seq.desc()).limit(limit)).all()
        return [self._row(r) for r in rows]

    def record_send(self, evaluation_id: str, approved_by: str) -> tuple[EntryRef, list]:
        """The manager approves sending. Messages go to the simulated outbox; nothing leaves the system."""
        from .notify import NotificationPackage, outbox
        if not approved_by.strip():
            raise RecordError("approved_by is required")
        events = self.timeline(evaluation_id)
        decision = self.latest_decision(evaluation_id)
        packages = [e for e in events if e["kind"] == "notification_package" and e["seq"] > decision["seq"]]
        if not packages:
            raise RecordError("no notification package for the latest decision; build one first")
        pkg_entry = packages[-1]
        pkg = NotificationPackage.model_validate(pkg_entry["payload"]["package"])
        msgs = outbox(pkg)
        ref = self._append("notifications_sent", decision["facility_id"],
                           {"package_hash": pkg_entry["hash"], "decision_hash": decision["hash"],
                            "mode": "simulated", "messages": [m.model_dump(mode="json") for m in msgs]},
                           approved_by.strip(), evaluation_id=evaluation_id, day=decision["day"],
                           posture=pkg.decided.posture)
        return ref, msgs

    # ---------- reading ----------

    def _row(self, r) -> dict:
        d = dict(r._mapping)
        d["payload"] = json.loads(d["entry"])["payload"]
        return d

    def _get_entry(self, entry_id: str, kind: str | None = None) -> dict:
        with self.engine.connect() as c:
            r = c.execute(sa.select(ledger).where(ledger.c.id == entry_id)).first()
        if r is None or (kind and r.kind != kind):
            raise RecordError(f"no {kind or 'entry'} {entry_id}")
        return self._row(r)

    def rule_set(self, version: str) -> str:
        with self.engine.connect() as c:
            content = c.execute(sa.select(rule_sets.c.content).where(rule_sets.c.version == version)).scalar()
        if content is None:
            raise RecordError(f"no rule set {version}")
        return content

    def timeline(self, evaluation_id: str) -> list[dict]:
        """Every event about one evaluation, in the order it was recorded."""
        with self.engine.connect() as c:
            rows = c.execute(sa.select(ledger).where(ledger.c.evaluation_id == evaluation_id)
                             .order_by(ledger.c.seq)).all()
        return [self._row(r) for r in rows]

    def load(self, evaluation_id: str) -> dict:
        """Everything recorded about an evaluation, rebuilt into the app's models."""
        events = self.timeline(evaluation_id)
        if not events or events[0]["kind"] != "evaluation":
            raise RecordError(f"no evaluation {evaluation_id}")
        ev = events[0]
        reports = [e for e in events if e["kind"] == "agent_report"]
        return {
            "evaluation": ev,
            "inputs": Inputs.model_validate(ev["payload"]["inputs"]),
            "recommendation": Recommendation.model_validate(ev["payload"]["recommendation"]),
            "agent_report": AgentReport.model_validate(reports[-1]["payload"]["report"]) if reports else None,
            "questions": [e for e in events if e["kind"] == "question"],
            "decisions": [e for e in events if e["kind"] == "decision"],
            "packages": [e for e in events if e["kind"] == "notification_package"],
            "sends": [e for e in events if e["kind"] == "notifications_sent"],
            "events": events,
        }

    def history(self, facility_id: str | None = None, limit: int = 50) -> list[dict]:
        """Evaluations newest first, each with its latest decision and agent status, for the history screen."""
        q = sa.select(ledger.c.id, ledger.c.seq, ledger.c.at, ledger.c.facility_id, ledger.c.day,
                      ledger.c.posture, ledger.c.hash).where(ledger.c.kind == "evaluation")
        if facility_id:
            q = q.where(ledger.c.facility_id == facility_id)
        with self.engine.connect() as c:
            evs = c.execute(q.order_by(ledger.c.seq.desc()).limit(limit)).all()
            ids = [e.id for e in evs]
            follow = c.execute(sa.select(ledger.c.evaluation_id, ledger.c.kind, ledger.c.actor, ledger.c.at,
                                         ledger.c.posture, ledger.c.entry)
                               .where(ledger.c.evaluation_id.in_(ids), ledger.c.kind != "evaluation")
                               .order_by(ledger.c.seq)).all() if ids else []
        out = []
        for e in evs:
            mine = [f for f in follow if f.evaluation_id == e.id]
            decision = next((f for f in reversed(mine) if f.kind == "decision"), None)
            dp = json.loads(decision.entry)["payload"] if decision else None
            out.append({"evaluation_id": e.id, "seq": e.seq, "at": e.at, "facility_id": e.facility_id,
                        "day": e.day, "recommended": e.posture, "hash": e.hash,
                        "agent": any(f.kind == "agent_report" for f in mine),
                        "questions": sum(f.kind == "question" for f in mine),
                        "decision": None if not decision else {
                            "action": dp["action"], "posture": dp["posture"], "label": dp["posture_label"],
                            "by": decision.actor, "at": decision.at, "reason": dp["reason"]}})
        return out

    def raw_body(self, fetch_id: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(sa.select(raw_fetches.c.body_gz).where(raw_fetches.c.id == fetch_id)).first()
        if r is None:
            raise RecordError(f"no raw fetch {fetch_id}")
        return json.loads(gzip.decompress(r.body_gz))

    # ---------- erasing (sysadmin only) ----------

    def counts(self) -> dict:
        with self.engine.connect() as c:
            kinds = dict(c.execute(sa.select(ledger.c.kind, sa.func.count()).group_by(ledger.c.kind)).all())
            raws = c.execute(sa.select(sa.func.count()).select_from(raw_fetches)).scalar()
            head = c.execute(sa.select(ledger.c.hash).order_by(ledger.c.seq.desc()).limit(1)).scalar()
        return {"entries": sum(kinds.values()), "by_kind": kinds, "raw_responses": raws, "head": head or GENESIS}

    def erase_all(self, erased_by: str, reason: str) -> EntryRef:
        """Drops and recreates the record (the triggers block DELETE and TRUNCATE, not DROP), then starts
        the new chain with a `record_erased` entry saying who erased what, when and why, and the hash of
        the old chain's last entry. History can be wiped, but never silently. Accounts are kept."""
        if len(reason.strip()) < 10:
            raise RecordError("give a reason of at least 10 characters")
        before = self.counts()
        details = {"reason": reason.strip(), "previous_entries": before["entries"],
                   "previous_by_kind": before["by_kind"], "previous_raw_responses": before["raw_responses"],
                   "previous_head": before["head"]}
        with self._lock:
            with self.engine.begin() as c:
                meta.drop_all(c)
                if self.dialect == "postgresql":
                    c.exec_driver_sql("DROP FUNCTION IF EXISTS closure_call_append_only()")
            meta.create_all(self.engine)
            self._install_triggers()
            # The admin log survives every erasure, so erasures chain there; the new record links to it.
            a = self.audit.append("record_erased", erased_by, "decision record", details)
            return self._append("record_erased", "all", {**details, "admin_log_seq": a["seq"],
                                                         "admin_log_hash": a["hash"]}, erased_by)

    def erasures(self) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(sa.select(ledger).where(ledger.c.kind == "record_erased").order_by(ledger.c.seq)).all()
        return [self._row(r) for r in rows]

    # ---------- verification ----------

    def verify_chain(self) -> dict:
        """Recomputes every hash. Any edited, removed or reordered row, raw response or rule set shows up."""
        problems = []
        prev = GENESIS
        n = 0
        with self.engine.connect() as c:
            for r in c.execute(sa.select(ledger).order_by(ledger.c.seq)):
                n += 1
                if r.prev_hash != prev:
                    problems.append(f"seq {r.seq}: prev_hash does not match the previous row (row removed or reordered)")
                if sha256(r.prev_hash + r.entry) != r.hash:
                    problems.append(f"seq {r.seq}: content does not match its hash (row edited)")
                e = json.loads(r.entry)
                for col in ("id", "kind", "facility_id", "evaluation_id", "actor", "day", "posture", "at"):
                    if e.get(col) != getattr(r, col):
                        problems.append(f"seq {r.seq}: column {col} differs from the hashed entry")
                for m in e["payload"].get("raw_fetches", []):
                    body = c.execute(sa.select(raw_fetches.c.body_gz).where(raw_fetches.c.id == m["id"])).scalar()
                    if body is None or sha256(gzip.decompress(body)) != m["sha256"]:
                        problems.append(f"seq {r.seq}: raw response {m['id']} ({m['url'][:60]}) missing or altered")
                prev = r.hash
            for v in c.execute(sa.select(rule_sets)):
                if rules_version(v.content) != v.version:
                    problems.append(f"rule set {v.version}: content does not match its version hash")
        return {"ok": not problems, "entries": n, "head": prev, "problems": problems}

    def replay(self, evaluation_id: str) -> dict:
        """Re-runs the rules engine on the recorded inputs with the recorded rule set; it must match exactly."""
        rec_entry = self._get_entry(evaluation_id, "evaluation")
        p = rec_entry["payload"]
        content = self.rule_set(p["rules_version"])
        again = evaluate(Inputs.model_validate(p["inputs"]), json.loads(content), p["rules_version"])
        recorded, now = p["recommendation"], again.model_dump(mode="json")
        diffs = sorted(k for k in recorded if canonical(recorded[k]) != canonical(now.get(k)))
        return {"matches": not diffs, "recorded_posture": recorded["posture"], "replayed_posture": again.posture,
                "rules_version": p["rules_version"], "differences": diffs}


LOCAL_DB = f"sqlite:///{(ROOT / 'data' / 'closure_call.db').as_posix()}"
_stores: dict[str | None, Store] = {}


def get_store(url: str | None = None) -> Store:
    """The configured record (DATABASE_URL), or url; "local" means the SQLite file, for development runs."""
    url = LOCAL_DB if url == "local" else url
    if url not in _stores:
        _stores[url] = Store(url)
    return _stores[url]
