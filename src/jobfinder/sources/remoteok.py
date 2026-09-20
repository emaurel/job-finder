"""RemoteOK. Free, no key. Their ToS asks for a do-follow link back if you
republish their listings - we only consume them privately, no republishing."""
from __future__ import annotations

from typing import Iterable

from ..models import Job
from .base import get_json

API = "https://remoteok.com/api"


class RemoteOK:
    name = "remoteok"

    def enabled(self) -> bool:
        return True

    def fetch(self) -> Iterable[Job]:
        # RemoteOK takes a SINGLE `tag`; the plural `tags=a,b,c` silently
        # returns an empty feed, which is how this source sat dead at 0 jobs.
        # The plain feed plus a few tags is wider than any tag filter, and our
        # own role filter decides relevance anyway.
        seen: set[str] = set()
        entries: list[dict] = []
        for params in (None, {"tag": "python"}, {"tag": "machine-learning"},
                       {"tag": "ai"}, {"tag": "engineer"}):
            data = get_json(API, params=params)
            if not isinstance(data, list):
                continue
            for entry in data:
                if isinstance(entry, dict) and entry.get("id") not in seen:
                    seen.add(entry.get("id"))
                    entries.append(entry)

        for entry in entries:
            # First element is a legal/terms notice, not a job.
            if not isinstance(entry, dict) or not entry.get("id") or not entry.get("position"):
                continue
            salary = ""
            if entry.get("salary_min") and entry.get("salary_max"):
                salary = f"${int(entry['salary_min']):,} - ${int(entry['salary_max']):,} / year"
            yield Job(
                source=self.name,
                external_id=str(entry["id"]),
                title=entry.get("position", ""),
                company=entry.get("company", ""),
                location=entry.get("location") or "Remote (Worldwide)",
                url=entry.get("url") or f"https://remoteok.com/remote-jobs/{entry.get('slug', entry['id'])}",
                description=entry.get("description", ""),
                salary=salary,
                posted_at=entry.get("date", ""),
                is_remote=True,
                raw={k: v for k, v in entry.items() if k != "description"},
            )
