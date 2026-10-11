"""Browser push notifications (Web Push with VAPID keys) to facility managers' devices.

A device subscribes from the account page; the browser's own push service (Google, Apple, Mozilla)
delivers messages, so no third-party account is needed. Subscriptions live in their own table, kept
apart from the decision record. Push services answer 404 or 410 for a subscription that no longer
exists; those are removed automatically.
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa

from .auth import users

log = logging.getLogger("closure_call.push")

push_meta = sa.MetaData()
subscriptions = sa.Table(
    "push_subscriptions", push_meta,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("username", sa.String(64), nullable=False, index=True),
    sa.Column("endpoint", sa.Text, nullable=False, unique=True),
    sa.Column("p256dh", sa.String(200), nullable=False),
    sa.Column("auth", sa.String(100), nullable=False),
    sa.Column("device", sa.String(200), nullable=False, default=""),
    sa.Column("created_at", sa.String(40), nullable=False),
)


def public_key() -> str:
    return os.environ.get("VAPID_PUBLIC_KEY", "")


def configured() -> bool:
    return bool(public_key() and os.environ.get("VAPID_PRIVATE_KEY"))


def describe_device(user_agent: str) -> str:
    ua = user_agent or ""
    os_name = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                   ("Windows", "Windows"), ("Mac OS", "Mac"), ("Linux", "Linux")) if k in ua), "Device")
    browser = next((n for k, n in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                                   ("Safari/", "Safari")) if k in ua), "browser")
    return f"{os_name} {browser}"


class Push:
    def __init__(self, engine: sa.Engine):
        self.engine = engine
        push_meta.create_all(engine)

    def subscribe(self, username: str, sub: dict, user_agent: str = "") -> None:
        endpoint, keys = sub.get("endpoint", ""), sub.get("keys") or {}
        if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("not a valid push subscription")
        with self.engine.begin() as c:
            c.execute(subscriptions.delete().where(subscriptions.c.endpoint == endpoint))
            c.execute(subscriptions.insert().values(
                id=str(uuid.uuid4()), username=username, endpoint=endpoint, p256dh=keys["p256dh"], auth=keys["auth"],
                device=describe_device(user_agent)[:200], created_at=datetime.now(timezone.utc).isoformat(timespec="seconds")))

    def remove(self, username: str, sub_id: str) -> None:
        with self.engine.begin() as c:
            c.execute(subscriptions.delete().where(subscriptions.c.id == sub_id, subscriptions.c.username == username))

    def devices(self, username: str) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(sa.select(subscriptions.c.id, subscriptions.c.device, subscriptions.c.created_at)
                             .where(subscriptions.c.username == username).order_by(subscriptions.c.created_at)).all()
        return [dict(r._mapping) for r in rows]

    def _send(self, rows, payload: dict) -> dict:
        from pywebpush import WebPushException, webpush
        sent, failed, removed = 0, 0, 0
        for r in rows:
            try:
                webpush(subscription_info={"endpoint": r.endpoint, "keys": {"p256dh": r.p256dh, "auth": r.auth}},
                        data=json.dumps(payload), vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                        vapid_claims={"sub": os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")},
                        ttl=6 * 3600, timeout=10)
                sent += 1
            except WebPushException as e:
                status = getattr(e.response, "status_code", None)
                if status in (404, 410):  # the device unsubscribed or the subscription expired
                    with self.engine.begin() as c:
                        c.execute(subscriptions.delete().where(subscriptions.c.id == r.id))
                    removed += 1
                else:
                    failed += 1
                    log.warning("push to %s failed: %s", r.device, str(e)[:200])
            except Exception as e:  # network trouble must never break the monitor
                failed += 1
                log.warning("push to %s failed: %s", r.device, str(e)[:200])
        return {"sent": sent, "failed": failed, "expired_removed": removed}

    def send_to_user(self, username: str, payload: dict) -> dict:
        if not configured():
            return {"sent": 0, "failed": 0, "expired_removed": 0, "note": "push not configured"}
        with self.engine.connect() as c:
            rows = c.execute(sa.select(subscriptions).where(subscriptions.c.username == username)).all()
        return self._send(rows, payload)

    def send_to_managers(self, payload: dict) -> dict:
        """Every device of every active manager account."""
        if not configured():
            return {"sent": 0, "failed": 0, "expired_removed": 0, "note": "push not configured"}
        with self.engine.connect() as c:
            rows = c.execute(sa.select(subscriptions).join(users, users.c.username == subscriptions.c.username)
                             .where(users.c.role == "manager", users.c.disabled.is_(False))).all()
        return self._send(rows, payload)
