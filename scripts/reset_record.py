"""Erases the whole decision record and starts an empty one. Irreversible.

Same operation as "Erase history" on the sysadmin page: the new chain starts with a `record_erased`
entry stating who erased it, why, how much was erased, and the hash of the old chain's last entry.
Accounts are kept.

Run from the repo root:
    python scripts/reset_record.py --db local                      # show what would be erased
    python scripts/reset_record.py --db local --reason "Clear test runs before demo" --yes-erase-everything
    python scripts/reset_record.py --reason "..." --yes-erase-everything   # the DATABASE_URL record (Akamai)
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.record import RecordError, get_store  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--db", help="'local' for the SQLite file instead of DATABASE_URL")
ap.add_argument("--reason", default="", help="why; recorded in the new chain's first entry")
ap.add_argument("--yes-erase-everything", action="store_true")
args = ap.parse_args()

store = get_store(args.db)
c = store.counts()
print(f"Record: {store.url_safe.split('@')[-1]}\n  ledger entries: {c['by_kind'] or 'none'}\n"
      f"  raw responses: {c['raw_responses']}")
if not args.yes_erase_everything:
    sys.exit("Nothing erased. Re-run with --reason \"...\" --yes-erase-everything to erase all of the above.")
try:
    ref = store.erase_all(f"{getpass.getuser()} (command line)", args.reason)
except RecordError as e:
    sys.exit(f"Nothing erased: {e}")
print(f"Erased. The new record starts with a record_erased entry (seq {ref.seq}). {store.verify_chain()}")
