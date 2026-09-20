"""Arbetsformedlingen / JobTech Dev. Free, official, no key. Best Sweden source."""
from __future__ import annotations

from typing import Iterable

from ..models import Job
from .base import get_json

API = "https://jobsearch.api.jobtechdev.se/search"

QUERIES = [
    "AI engineer",
    "machine learning engineer",
    "LLM",
    "python utvecklare",
    "backend developer",
]


class JobTech:
    name = "jobtech"

    def enabled(self) -> bool:
        return True

    def fetch(self) -> Iterable[Job]:
        seen: set[str] = set()
        for query in QUERIES:
            data = get_json(API, params={"q": query, "limit": 50, "offset": 0})
            if not data:
                continue
            for hit in data.get("hits", []):
                url = hit.get("webpage_url") or (hit.get("application_details") or {}).get("url") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                addr = hit.get("workplace_address") or {}
                location = ", ".join(x for x in (addr.get("municipality"), addr.get("region")) if x) or "Sweden"
                hours = ((hit.get("working_hours_type") or {}).get("label") or "").lower()
                remote = hit.get("remote_work") is True or "distans" in hours or "distans" in location.lower()
                yield Job(
                    source=self.name,
                    external_id=str(hit.get("id", "")),
                    title=hit.get("headline", ""),
                    company=(hit.get("employer") or {}).get("name", ""),
                    location=location,
                    url=url,
                    description=(hit.get("description") or {}).get("text", ""),
                    salary=(hit.get("salary_description") or "") or "See listing",
                    posted_at=hit.get("publication_date", ""),
                    is_remote=bool(remote),
                    apply_email=((hit.get("application_details") or {}).get("email") or ""),
                    raw=hit,
                )
