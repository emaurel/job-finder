"""Company ATS boards: Greenhouse, Lever, Ashby.

These matter more than the aggregators. They are the employer's own board, so
the listing is current, the description is complete, and the apply path is a
structured form rather than a LinkedIn redirect. No API key, no ToS problem.

Add companies to companies.yaml as you find ones worth watching.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml

from ..config import ROOT
from ..models import Job
from .base import get_json

COMPANIES_PATH = ROOT / "profile" / "companies.yaml"


def _companies() -> dict[str, list[str]]:
    if not COMPANIES_PATH.exists():
        return {}
    with COMPANIES_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class Greenhouse:
    name = "greenhouse"

    def enabled(self) -> bool:
        return bool(_companies().get("greenhouse"))

    def fetch(self) -> Iterable[Job]:
        for slug in _companies().get("greenhouse", []):
            data = get_json(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                params={"content": "true"},
            )
            if not data:
                continue
            for job in data.get("jobs", []):
                loc = (job.get("location") or {}).get("name", "")
                yield Job(
                    source=self.name,
                    external_id=str(job.get("id", "")),
                    title=job.get("title", ""),
                    company=slug.replace("-", " ").title(),
                    location=loc,
                    url=job.get("absolute_url", ""),
                    description=job.get("content", ""),
                    posted_at=job.get("updated_at", ""),
                    is_remote="remote" in loc.lower(),
                    ats="greenhouse",
                    raw={"board": slug, "id": job.get("id")},
                )


class Lever:
    name = "lever"

    def enabled(self) -> bool:
        return bool(_companies().get("lever"))

    def fetch(self) -> Iterable[Job]:
        for slug in _companies().get("lever", []):
            data = get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
            if not isinstance(data, list):
                continue
            for job in data:
                cats = job.get("categories") or {}
                loc = cats.get("location", "") or ""
                yield Job(
                    source=self.name,
                    external_id=str(job.get("id", "")),
                    title=job.get("text", ""),
                    company=slug.replace("-", " ").title(),
                    location=loc,
                    url=job.get("hostedUrl", ""),
                    apply_url=job.get("applyUrl", ""),
                    description=job.get("descriptionPlain") or job.get("description", ""),
                    salary=cats.get("commitment", ""),
                    posted_at=str(job.get("createdAt", "")),
                    is_remote="remote" in loc.lower(),
                    ats="lever",
                    raw={"board": slug, "id": job.get("id")},
                )


class Ashby:
    name = "ashby"

    def enabled(self) -> bool:
        return bool(_companies().get("ashby"))

    def fetch(self) -> Iterable[Job]:
        for slug in _companies().get("ashby", []):
            data = get_json(
                f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                params={"includeCompensation": "true"},
            )
            if not data:
                continue
            for job in data.get("jobs", []):
                comp = job.get("compensation") or {}
                summary = comp.get("compensationTierSummary") or ""
                yield Job(
                    source=self.name,
                    external_id=str(job.get("id", "")),
                    title=job.get("title", ""),
                    company=slug.replace("-", " ").title(),
                    location=job.get("location", ""),
                    url=job.get("jobUrl", ""),
                    apply_url=job.get("applyUrl") or job.get("jobUrl", ""),
                    description=job.get("descriptionPlain") or job.get("descriptionHtml", ""),
                    salary=summary,
                    posted_at=job.get("publishedAt", ""),
                    is_remote=bool(job.get("isRemote")),
                    ats="ashby",
                    raw={"board": slug, "id": job.get("id")},
                )
