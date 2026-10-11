"""Admin log: an append-only, hash-chained audit trail of account changes and record erasures.

It lives beside the decision record but in its own metadata, so erasing the record never drops it,
and its append-only triggers use their own function name, so dropping the record's trigger function
cannot touch them. Each entry's hash covers the previous entry's hash. After every append, the new head
hash is written to the server log ("anchoring"): someone with database credentials could rewrite the
table, but not the copies of past heads already in the logs.
"""
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from hashlib import sha256

import sqlalchemy as sa

log = logging.getLogger("closure_call.audit")
GENESIS = "0" * 64

audit_meta = sa.MetaData()
SeqType = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
admin_log = sa.Table(
    "admin_log", audit_meta,
    sa.Column("seq", SeqType, primary_key=True, autoincrement=True),
    sa.Column("id", sa.String(36), nullable=False, unique=True),
    sa.Column("at", sa.String(40), nullable=False),
    sa.Column("action", sa.String(40), nullable=False, index=True),
    sa.Column("actor", sa.String(160), nullable=False),
    sa.Column("target", sa.String(160), nullable=False),
    sa.Column("entry", sa.Text, nullable=False),
    sa.Column("prev_hash", sa.String(64), nullable=False),
    sa.Column("hash", sa.String(64), nullable=False, unique=True),
)


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class AuditLog:
    def __init__(self, engine: sa.Engine):
        self.engine = engine
        self.dialect = engine.dialect.name
        self._lock = threading.RLock()
        audit_meta.create_all(engine)
        with engine.begin() as c:
            if self.dialect == "postgresql":
                c.exec_driver_sql("""
                    CREATE OR REPLACE FUNCTION closure_call_admin_log_append_only() RETURNS trigger AS $$
                    BEGIN
                      RAISE EXCEPTION USING MESSAGE = 'admin log is append-only: ' || TG_OP || ' is not allowed';
                    END $$ LANGUAGE plpgsql""")
                for when, each in (("UPDATE OR DELETE", "ROW"), ("TRUNCATE", "STATEMENT")):
                    name = "admin_log_no_truncate" if when == "TRUNCATE" else "admin_log_append_only"
                    c.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name} ON admin_log")
                    c.exec_driver_sql(f"CREATE TRIGGER {name} BEFORE {when} ON admin_log FOR EACH {each} "
                                      f"EXECUTE FUNCTION closure_call_admin_log_append_only()")
            elif self.dialect == "sqlite":
                for op in ("UPDATE", "DELETE"):
                    c.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS admin_log_no_{op.lower()} BEFORE {op} ON admin_log "
                                      f"BEGIN SELECT RAISE(ABORT, 'admin log is append-only: {op} is not allowed'); END")

    def append(self, action: str, actor: str, target: str, details: dict | None = None) -> dict:
        header = {"id": str(uuid.uuid4()), "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "action": action, "actor": actor, "target": target}
        entry = _canonical({**header, "details": details or {}})
        with self._lock, self.engine.begin() as c:
            if self.dialect == "postgresql":
                c.exec_driver_sql("LOCK TABLE admin_log IN SHARE ROW EXCLUSIVE MODE")
            prev = c.execute(sa.select(admin_log.c.hash).order_by(admin_log.c.seq.desc()).limit(1)).scalar() or GENESIS
            h = sha256((prev + entry).encode("utf-8")).hexdigest()
            seq = c.execute(admin_log.insert().values(**header, entry=entry, prev_hash=prev, hash=h)).inserted_primary_key[0]
        # Anchor: the server log keeps a copy of every head that a database admin cannot rewrite.
        log.warning("AUDIT ANCHOR seq=%s hash=%s action=%s actor=%s target=%s", seq, h, action, actor, target)
        return {**header, "seq": seq, "hash": h, "prev_hash": prev}

    def entries(self, limit: int = 100) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(sa.select(admin_log).order_by(admin_log.c.seq.desc()).limit(limit)).all()
        return [{**dict(r._mapping), "details": json.loads(r.entry)["details"]} for r in rows]

    def verify(self) -> dict:
        problems, prev, n = [], GENESIS, 0
        with self.engine.connect() as c:
            for r in c.execute(sa.select(admin_log).order_by(admin_log.c.seq)):
                n += 1
                if r.prev_hash != prev:
                    problems.append(f"admin log #{r.seq}: does not follow the previous entry (removed or reordered)")
                if sha256((r.prev_hash + r.entry).encode("utf-8")).hexdigest() != r.hash:
                    problems.append(f"admin log #{r.seq}: content does not match its hash (edited)")
                e = json.loads(r.entry)
                if any(e.get(k) != getattr(r, k) for k in ("id", "at", "action", "actor", "target")):
                    problems.append(f"admin log #{r.seq}: a column differs from the hashed entry")
                prev = r.hash
        return {"ok": not problems, "entries": n, "head": prev, "problems": problems}
