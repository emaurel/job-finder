"""Source adapter contract plus a shared HTTP helper."""
from __future__ import annotations

import logging
from typing import Iterable, Protocol

import httpx2 as httpx

from ..models import Job

log = logging.getLogger(__name__)

USER_AGENT = "JobFinderBot/2.0 (+https://emaurel.github.io)"
TIMEOUT = 30.0


class Source(Protocol):
    name: str

    def enabled(self) -> bool: ...
    def fetch(self) -> Iterable[Job]: ...


def get_json(url: str, *, params: dict | None = None, headers: dict | None = None):
    """GET returning parsed JSON, or None on any failure. Never raises."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    try:
        resp = httpx.get(url, params=params, headers=hdrs, timeout=TIMEOUT,
                         follow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - a dead source must not kill the run
        log.warning("fetch failed %s: %s", url, exc)
        return None


def get_json_with_quota(url: str, *, params: dict | None = None,
                        headers: dict | None = None) -> tuple[object, int | None]:
    """Like get_json, but also reports the API's remaining monthly allowance.

    RapidAPI returns it in x-ratelimit-requests-remaining. Knowing it lets a
    source stop before it burns a quota the user paid for or relies on.
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    try:
        resp = httpx.get(url, params=params, headers=hdrs, timeout=TIMEOUT,
                         follow_redirects=True)
        resp.raise_for_status()
        raw = resp.headers.get("x-ratelimit-requests-remaining")
        return resp.json(), (int(raw) if raw and raw.isdigit() else None)
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch failed %s: %s", url, exc)
        return None, None
