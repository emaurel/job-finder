"""Keyed aggregators: JSearch (RapidAPI) and Adzuna. Both optional."""
from __future__ import annotations

from typing import Iterable

import logging

from ..config import env
from ..models import Job
from .base import get_json, get_json_with_quota

log = logging.getLogger(__name__)

# JSearch is the only legitimate way into LinkedIn and Indeed: neither has a
# public jobs API, and scraping them is a ToS violation that costs the account.
# The free RapidAPI plan allows 200 requests a MONTH, and one call counts as
# one request however many pages it returns. At ~22 weekday runs that is about
# 8 calls per run, so these queries are chosen, not sprayed.
JSEARCH_QUERIES = [
    {"query": "AI engineer LLM remote europe", "remote_jobs_only": "true"},
    {"query": "machine learning engineer remote europe", "remote_jobs_only": "true"},
    {"query": "python backend engineer remote europe", "remote_jobs_only": "true"},
    {"query": "LLM agent engineer remote", "remote_jobs_only": "true"},
    {"query": "typescript backend engineer remote europe", "remote_jobs_only": "true"},
    {"query": "AI engineer Sweden", "date_posted": "month"},
]

# Stop before the monthly allowance is gone, so a runaway loop cannot burn it.
JSEARCH_MIN_REMAINING = 20


class JSearch:
    name = "jsearch"

    def enabled(self) -> bool:
        return bool(env("JSEARCH_API_KEY"))

    def fetch(self) -> Iterable[Job]:
        key = env("JSEARCH_API_KEY")
        headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
        for base in JSEARCH_QUERIES:
            params = {"num_pages": "3", "date_posted": "month", **base}
            data, remaining = get_json_with_quota(
                "https://jsearch.p.rapidapi.com/search", params=params, headers=headers)
            if remaining is not None and remaining < JSEARCH_MIN_REMAINING:
                log.warning("JSearch quota down to %s requests this month, stopping early",
                            remaining)
                if data:
                    yield from self._parse(data)
                return
            if not data:
                continue
            yield from self._parse(data)

    def _parse(self, data: dict) -> Iterable[Job]:
            for job in data.get("data", []):
                url = job.get("job_apply_link") or job.get("job_google_link") or ""
                if not url:
                    continue
                remote = bool(job.get("job_is_remote"))
                loc = "Remote" if remote else ", ".join(
                    x for x in (job.get("job_city"), job.get("job_state"), job.get("job_country")) if x)
                salary = ""
                if job.get("job_min_salary") and job.get("job_max_salary"):
                    cur = job.get("job_salary_currency") or "USD"
                    period = job.get("job_salary_period") or "year"
                    salary = f"{cur} {int(job['job_min_salary']):,} - {int(job['job_max_salary']):,} / {period}"
                yield Job(
                    source=self.name,
                    external_id=str(job.get("job_id", "")),
                    title=job.get("job_title", ""),
                    company=job.get("employer_name", ""),
                    location=loc or "Unknown",
                    url=url,
                    description=job.get("job_description", ""),
                    salary=salary,
                    posted_at=job.get("job_posted_at_datetime_utc", ""),
                    is_remote=remote,
                    apply_email=job.get("job_apply_is_direct") and "" or "",
                    raw={"publisher": job.get("job_publisher")},
                )


class Adzuna:
    name = "adzuna"

    def enabled(self) -> bool:
        return bool(env("ADZUNA_APP_ID") and env("ADZUNA_APP_KEY"))

    def fetch(self) -> Iterable[Job]:
        app_id, app_key = env("ADZUNA_APP_ID"), env("ADZUNA_APP_KEY")
        # Adzuna is country-scoped; se = Sweden, gb/de/fr for wider EU remote.
        for country in ("gb", "fr", "de", "nl", "es"):  # Adzuna has no 'se' endpoint
            data = get_json(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                params={"app_id": app_id, "app_key": app_key, "results_per_page": 50,
                        "what": "AI engineer machine learning", "max_days_old": 14,
                        "content-type": "application/json"},
            )
            if not data:
                continue
            for job in data.get("results", []):
                loc = (job.get("location") or {}).get("display_name", "")
                salary = ""
                if job.get("salary_min") and job.get("salary_max"):
                    salary = f"{int(job['salary_min']):,} - {int(job['salary_max']):,} / year"
                yield Job(
                    source=self.name,
                    external_id=str(job.get("id", "")),
                    title=job.get("title", ""),
                    company=(job.get("company") or {}).get("display_name", ""),
                    location=loc,
                    url=job.get("redirect_url", ""),
                    description=job.get("description", ""),
                    salary=salary,
                    posted_at=job.get("created", ""),
                    is_remote="remote" in f"{job.get('title','')} {loc}".lower(),
                    raw={"country": country},
                )
