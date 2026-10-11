"""Erases the whole decision record and starts an empty one. Irreversible.

The ledger's triggers block DELETE and TRUNCATE by design, so this drops and recreates the tables
instead. Meant for wiping development and test runs before a demo, never for editing history.

Run from the repo root:
    python scripts/reset_record.py --db local                     # show what would be erased
    python scripts/reset_record.py --db local --yes-erase-everything
    python scripts/reset_record.py --yes-erase-everything        # the DATABASE_URL record (Akamai)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlalchemy as sa  # noqa: E402

from app.record import Store, get_store, ledger, meta, raw_fetches  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--db", help="'local' for the SQLite file instead of DATABASE_URL")
ap.add_argument("--yes-erase-everything", action="store_true")
args = ap.parse_args()

store = get_store(args.db)
where = store.url_safe.split("@")[-1]
with store.engine.connect() as c:
    kinds = dict(c.execute(sa.select(ledger.c.kind, sa.func.count()).group_by(ledger.c.kind)).all())
    raws = c.execute(sa.select(sa.func.count()).select_from(raw_fetches)).scalar()
print(f"Record: {where}\n  ledger entries: {kinds or 'none'}\n  raw responses: {raws}")

if not args.yes_erase_everything:
    sys.exit("Nothing erased. Re-run with --yes-erase-everything to erase all of the above.")

with store.engine.begin() as c:
    meta.drop_all(c)  # DROP TABLE is not blocked by the row-level append-only triggers
    if store.dialect == "postgresql":
        c.exec_driver_sql("DROP FUNCTION IF EXISTS closure_call_append_only()")
store.engine.dispose()
fresh = Store(store.engine.url.render_as_string(hide_password=False))  # recreates tables and triggers
print(f"Erased. New empty record: {fresh.verify_chain()}")
