"""Manage Closure Call accounts. Passwords are always typed at a prompt (never on the command line).

Run from the repo root (uses DATABASE_URL, so this manages the live accounts; add --db local for dev):
    python scripts/users.py add luke --name "Luke Dunn" --role manager
    python scripts/users.py list
    python scripts/users.py passwd luke
    python scripts/users.py disable luke        (and: enable luke)
Roles: manager (check, re-pin, decide, send), reviewer (read, verify, replay, ask), and sysadmin
(manage accounts, erase history; cannot decide). Sysadmins can also do all of this on the /admin page.
On Windows, run this in PowerShell or cmd: Git Bash cannot hide what you type at the password prompt.
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import ROLES, Accounts, AuthError  # noqa: E402
from app.record import get_store  # noqa: E402


def ask_password() -> str:
    first = getpass.getpass("New password (at least 10 characters): ")
    if getpass.getpass("Repeat it: ") != first:
        sys.exit("Passwords did not match; nothing changed.")
    return first


ap = argparse.ArgumentParser()
ap.add_argument("command", choices=["add", "list", "passwd", "disable", "enable"])
ap.add_argument("username", nargs="?")
ap.add_argument("--name", help="display name shown in the decision record")
ap.add_argument("--role", choices=ROLES, default="manager")
ap.add_argument("--db", help="'local' for the SQLite file instead of DATABASE_URL")
args = ap.parse_args()

store = get_store(args.db)
accounts = Accounts(store.engine)
print(f"Accounts in {store.url_safe.split('@')[-1]}")
try:
    if args.command == "list":
        for u in accounts.list():
            print(f"  {u['username']:16} {u['role']:9} {u['display_name']:24} "
                  f"{'DISABLED' if u['disabled'] else 'active'}  created {u['created_at']}")
        if not accounts.count():
            print("  (none)")
    elif not args.username:
        sys.exit("username is required")
    elif args.command == "add":
        accounts.add(args.username, args.name or args.username, args.role, ask_password())
        print(f"Added {args.username} ({args.role}).")
    elif args.command == "passwd":
        accounts.set_password(args.username, ask_password())
        print(f"Password changed for {args.username}; their existing sessions are signed out.")
    else:
        accounts.set_disabled(args.username, args.command == "disable")
        print(f"{args.username} {args.command}d.")
except AuthError as e:
    sys.exit(f"Not done: {e}")
