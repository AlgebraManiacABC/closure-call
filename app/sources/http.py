"""Shared HTTP fetch that keeps the raw response, its source and the time it was fetched."""
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

# NWS asks every client to identify itself with a contact in the User-Agent.
USER_AGENT = os.environ.get("NWS_USER_AGENT") or "closure-call (Hack Midwest 2026)"

_client = httpx.Client(
    timeout=20.0,
    headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"},
    follow_redirects=True,
)


@dataclass(frozen=True)
class Fetched:
    source: str  # "nws", "nwps" or "usgs"
    url: str  # final URL including query string
    fetched_at: datetime  # UTC
    status: int
    body: Any  # parsed JSON


class SourceError(RuntimeError):
    pass


def get_json(source: str, url: str, params: dict | None = None, retries: int = 2) -> Fetched:
    """GET a JSON resource. Retries server errors and timeouts, which NWS returns now and then."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = _client.get(url, params=params)
            if resp.status_code < 500:
                if resp.status_code >= 400:
                    raise SourceError(f"{source} {resp.status_code} for {resp.url}: {resp.text[:300]}")
                return Fetched(source, str(resp.url), datetime.now(timezone.utc), resp.status_code, resp.json())
            last_error = SourceError(f"{source} {resp.status_code} for {resp.url}")
        except httpx.TransportError as e:
            last_error = SourceError(f"{source} transport error for {url}: {e}")
        time.sleep(1.5 * (attempt + 1))
    raise last_error
