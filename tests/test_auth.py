"""Sign-in, sessions, roles and identity in the record, on a throwaway SQLite database (no network, no model).

Run from the repo root:  python tests/test_auth.py
"""
import os
import secrets
import sys
import tempfile
from pathlib import Path

DB = f"sqlite:///{(Path(tempfile.mkdtemp()) / 'auth.db').as_posix()}"
os.environ["DB"] = DB
os.environ["SESSION_SECRET"] = secrets.token_urlsafe(32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging  # noqa: E402

logging.disable(logging.INFO)

from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.rules import evaluate  # noqa: E402
from app.web import accounts, app, store  # noqa: E402
from test_rules import gusts, make  # noqa: E402

PW = {"mgr": secrets.token_urlsafe(12), "rev": secrets.token_urlsafe(12)}  # test-only, generated per run
accounts.add("mgr", "Test Manager", "manager", PW["mgr"])
accounts.add("rev", "Test Reviewer", "reviewer", PW["rev"])


def client(user: str | None = None) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    if user:
        r = c.post("/login", data={"username": user, "password": PW[user], "next": "/"})
        assert r.status_code == 303 and auth.SESSION_COOKIE in r.cookies, r.text
    return c


def test_passwords_are_salted_and_verified():
    h1, h2 = auth.hash_password("correct horse"), auth.hash_password("correct horse")
    assert h1 != h2 and auth.verify_password("correct horse", h1) and not auth.verify_password("wrong one!", h1)
    try:
        auth.hash_password("short")
        raise AssertionError("short password accepted")
    except auth.AuthError:
        pass


def test_everything_needs_sign_in_except_login_and_health():
    c = client()
    r = c.get("/manager")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/manager"
    assert c.get("/history").status_code == 303
    assert c.get("/e/x/status").status_code == 401
    assert c.get("/healthz").status_code == 200 and c.get("/login").status_code == 200


def test_wrong_password_and_tampered_cookie_are_rejected():
    c = client()
    r = c.post("/login", data={"username": "mgr", "password": "not-the-password", "next": "/"})
    assert "error=" in r.headers["location"] and auth.SESSION_COOKIE not in r.cookies
    good = client("mgr").cookies.get(auth.SESSION_COOKIE)
    body, sig = good.rsplit(".", 1)
    c.cookies.set(auth.SESSION_COOKIE, body + "." + ("0" if sig[0] != "0" else "1") + sig[1:])
    assert c.get("/manager").status_code == 303


def test_reviewer_can_read_but_not_act():
    inp = make(gusts([6, 7], 62))
    eid = store.record_evaluation(inp, evaluate(inp), []).id
    c = client("rev")
    assert c.get("/history").status_code == 200 and c.get(f"/e/{eid}").status_code == 200
    r = c.post(f"/e/{eid}/decide", data={"action": "approve"})
    assert "Only+a+facility+manager" in r.headers["location"] or "Only%20a%20facility%20manager" in r.headers["location"]
    assert not store.load(eid)["decisions"]
    assert "Approve:" not in c.get(f"/e/{eid}").text  # no decision form for reviewers


def test_decisions_record_the_signed_in_account():
    inp = make(gusts([6, 7], 62))
    eid = store.record_evaluation(inp, evaluate(inp), []).id
    c = client("mgr")
    c.post(f"/e/{eid}/decide", data={"action": "approve"})
    d = store.load(eid)["decisions"][-1]
    assert d["actor"] == "Test Manager (@mgr, manager)", d["actor"]
    assert store.verify_chain()["ok"]


def test_password_change_signs_out_existing_sessions():
    accounts.add("tmp", "Temp", "manager", "first-password-1")
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": "tmp", "password": "first-password-1", "next": "/"})
    assert c.get("/manager").status_code == 200
    accounts.set_password("tmp", "second-password-2")
    assert c.get("/manager").status_code == 303


def test_repeated_failures_lock_the_account_for_a_while():
    accounts.add("lock", "Lock", "manager", "lock-password-1")
    c = client()
    for _ in range(5):
        c.post("/login", data={"username": "lock", "password": "nope-nope-nope", "next": "/"})
    r = c.post("/login", data={"username": "lock", "password": "lock-password-1", "next": "/"})
    assert "Too+many" in r.headers["location"] or "Too%20many" in r.headers["location"]
    auth.failed_logins.clear("user:lock")
    auth.failed_logins.clear("ip:testclient")


PW["admin"] = secrets.token_urlsafe(12)
accounts.add("admin", "Test Admin", "sysadmin", PW["admin"])


def test_sysadmin_cannot_decide_and_others_cannot_administer():
    inp = make(gusts([6, 7], 62))
    eid = store.record_evaluation(inp, evaluate(inp), []).id
    admin = client("admin")
    admin.post(f"/e/{eid}/decide", data={"action": "approve"})
    assert not store.load(eid)["decisions"]  # separation of duties
    for who in ("mgr", "rev"):
        c = client(who)
        assert "error=" in c.get("/admin").headers["location"]
        r = c.post("/admin/erase", data={"confirm": "ERASE", "reason": "should not work", "password": PW[who]})
        assert "error=" in r.headers["location"]
    assert store.counts()["entries"] > 0


def test_sysadmin_manages_accounts():
    admin = client("admin")
    admin.post("/admin/users", data={"username": "newrev", "display_name": "New Reviewer", "role_name": "reviewer",
                                     "password": "starting-pass-1"})
    assert any(u["username"] == "newrev" and u["role"] == "reviewer" for u in accounts.list())
    admin.post("/admin/users/newrev/disabled", data={"disabled": "1"})
    assert accounts.authenticate("newrev", "starting-pass-1") is None
    r = admin.post("/admin/users/admin/disabled", data={"disabled": "1"})
    assert "error=" in r.headers["location"]  # cannot lock yourself out


def test_erase_needs_confirmation_and_password_and_leaves_a_trace():
    admin = client("admin")
    before = store.counts()
    for bad in ({"confirm": "erase", "reason": "clearing test runs", "password": PW["admin"]},
                {"confirm": "ERASE", "reason": "clearing test runs", "password": "wrong-password"},
                {"confirm": "ERASE", "reason": "short", "password": PW["admin"]}):
        admin.post("/admin/erase", data=bad)
        assert store.counts()["entries"] == before["entries"]
    admin.post("/admin/erase", data={"confirm": "ERASE", "reason": "clearing test runs", "password": PW["admin"]})
    after = store.counts()
    assert after["entries"] == 1 and list(after["by_kind"]) == ["record_erased"]
    e = store.erasures()[0]
    assert e["actor"] == "Test Admin (@admin, sysadmin)" and e["payload"]["previous_head"] == before["head"]
    assert store.verify_chain()["ok"] and accounts.get("mgr") is not None  # accounts survive
    assert "Earlier history was erased" in client("rev").get("/history").text


def test_users_change_their_own_password():
    accounts.add("self", "Self", "reviewer", "self-password-1")
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": "self", "password": "self-password-1", "next": "/"})
    r = c.post("/account/password", data={"current": "wrong-one-xx", "new": "self-password-2", "repeat": "self-password-2"})
    assert "error=" in r.headers["location"]
    c.post("/account/password", data={"current": "self-password-1", "new": "self-password-2", "repeat": "self-password-2"})
    assert accounts.authenticate("self", "self-password-2") is not None
    assert c.get("/history").status_code == 200  # this browser got a fresh session


def test_next_cannot_redirect_off_site():
    c = client()
    r = c.post("/login", data={"username": "mgr", "password": PW["mgr"], "next": "//evil.example/"})
    assert r.headers["location"] == "/"


if __name__ == "__main__":
    for k, f in dict(globals()).items():
        if k.startswith("test_"):
            f()
            print("ok", k)
