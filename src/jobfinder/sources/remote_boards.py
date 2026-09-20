"""Remote-first job boards. All free, all keyless, all allow API use.

These matter more for Edgar than the big aggregators: he is remote-only, and
these boards are remote-only, so the hit rate per posting is far higher.
"""
from __future__ import annotations

import re
from typing import Iterable

from ..models import Job
from .base import get_json


def _strip(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


class Remotive:
    name = "remotive"

    def enabled(self) -> bool:
        return True

    def fetch(self) -> Iterable[Job]:
        for category in ("software-dev", "data"):
            data = get_json("https://remotive.com/api/remote-jobs",
                            params={"category": category, "limit": 200})
            if not data:
                continue
            for job in data.get("jobs", []):
                yield Job(
                    source=self.name,
                    external_id=str(job.get("id", "")),
                    title=job.get("title", ""),
                    company=job.get("company_name", ""),
                    # This board states who it can hire, which is exactly what
                    # the geography filter needs.
                    location=job.get("candidate_required_location") or "Remote",
                    url=job.get("url", ""),
                    description=_strip(job.get("description", "")),
                    salary=job.get("salary", ""),
                    posted_at=job.get("publication_date", ""),
                    is_remote=True,
                    raw={"tags": job.get("tags", [])},
                )


class Jobicy:
    name = "jobicy"

    def enabled(self) -> bool:
        return True

    def fetch(self) -> Iterable[Job]:
        for industry in ("engineering", "data-science"):
            data = get_json("https://jobicy.com/api/v2/remote-jobs",
                            params={"count": 100, "industry": industry})
            if not data:
                continue
            for job in data.get("jobs", []):
                salary = ""
                if job.get("annualSalaryMin") and job.get("annualSalaryMax"):
                    salary = (f"{job.get('salaryCurrency', 'USD')} "
                              f"{job['annualSalaryMin']:,} - {job['annualSalaryMax']:,} / year")
                yield Job(
                    source=self.name,
                    external_id=str(job.get("id", "")),
                    title=job.get("jobTitle", ""),
                    company=job.get("companyName", ""),
                    location=job.get("jobGeo") or "Remote",
                    url=job.get("url", ""),
                    description=_strip(job.get("jobDescription") or job.get("jobExcerpt", "")),
                    salary=salary,
                    posted_at=job.get("pubDate", ""),
                    is_remote=True,
                    raw={"level": job.get("jobLevel")},
                )


class Himalayas:
    name = "himalayas"

    def enabled(self) -> bool:
        return True

    def fetch(self) -> Iterable[Job]:
        for offset in (0, 100):
            data = get_json("https://himalayas.app/jobs/api",
                            params={"limit": 100, "offset": offset})
            if not data:
                continue
            for job in data.get("jobs", []):
                restrictions = job.get("locationRestrictions") or []
                salary = ""
                if job.get("minSalary") and job.get("maxSalary"):
                    salary = (f"{job.get('currency', 'USD')} {int(job['minSalary']):,} - "
                              f"{int(job['maxSalary']):,} / {job.get('salaryPeriod', 'year')}")
                yield Job(
                    source=self.name,
                    external_id=str(job.get("guid") or job.get("title", "")),
                    title=job.get("title", ""),
                    company=job.get("companyName", ""),
                    location=", ".join(restrictions) if restrictions else "Remote (Worldwide)",
                    url=job.get("applicationLink") or job.get("url", ""),
                    description=_strip(job.get("description") or job.get("excerpt", "")),
                    salary=salary,
                    posted_at=str(job.get("pubDate", "")),
                    is_remote=True,
                    raw={"seniority": job.get("seniority")},
                )


class Arbeitnow:
    name = "arbeitnow"

    def enabled(self) -> bool:
        return True

    def fetch(self) -> Iterable[Job]:
        # EU board, Germany-heavy. Most of it is German-language and gets
        # dropped by the working-language filter; the English-language remote
        # tech roles are what we are here for.
        for page in (1, 2, 3):
            data = get_json("https://www.arbeitnow.com/api/job-board-api",
                            params={"page": page})
            if not data:
                continue
            for job in data.get("data", []):
                yield Job(
                    source=self.name,
                    external_id=job.get("slug", ""),
                    title=job.get("title", ""),
                    company=job.get("company_name", ""),
                    location=job.get("location") or "Germany",
                    url=job.get("url", ""),
                    description=_strip(job.get("description", "")),
                    posted_at=str(job.get("created_at", "")),
                    is_remote=bool(job.get("remote")),
                    raw={"tags": job.get("tags", [])},
                )
