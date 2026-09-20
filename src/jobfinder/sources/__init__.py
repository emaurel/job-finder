"""Source registry. Disabled sources (missing keys, empty company list) are skipped."""
from __future__ import annotations

import logging

from ..models import Job
from .aggregators import Adzuna, JSearch
from .ats import Ashby, Greenhouse, Lever
from .jobtech import JobTech
from .remote_boards import Arbeitnow, Himalayas, Jobicy, Remotive
from .remoteok import RemoteOK

log = logging.getLogger(__name__)

ALL_SOURCES = [
    JobTech(), RemoteOK(), Remotive(), Jobicy(), Himalayas(), Arbeitnow(),
    Greenhouse(), Lever(), Ashby(), JSearch(), Adzuna(),
]


def fetch_all(only: list[str] | None = None) -> list[Job]:
    jobs: list[Job] = []
    for source in ALL_SOURCES:
        if only and source.name not in only:
            continue
        if not source.enabled():
            log.info("skip %s (not configured)", source.name)
            continue
        try:
            found = list(source.fetch())
        except Exception as exc:  # noqa: BLE001
            log.warning("source %s failed: %s", source.name, exc)
            continue
        log.info("%s -> %d jobs", source.name, len(found))
        jobs.extend(found)
    return jobs
