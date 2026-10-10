"""Starts the web app. --db local uses the SQLite file instead of DATABASE_URL (for development).

Run from the repo root:  python scripts/serve.py [--db local] [--port 8000] [--host 0.0.0.0]
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

ap = argparse.ArgumentParser()
ap.add_argument("--db")
ap.add_argument("--port", type=int, default=8000)
ap.add_argument("--host", default="127.0.0.1")
args = ap.parse_args()
if args.db:
    os.environ["DB"] = args.db

import uvicorn  # noqa: E402

uvicorn.run("app.web:app", host=args.host, port=args.port)
