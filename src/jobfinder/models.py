"""Core data shapes shared across sources, scoring and delivery."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Job:
    """One posting, normalised across every source."""

    source: str
    external_id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    apply_url: str = ""
    salary: str = ""
    posted_at: str = ""
    is_remote: bool = False
    # How an application could actually be delivered.
    apply_email: str = ""
    ats: str = ""          # greenhouse | lever | ashby | ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = _clean(self.title) or "Unknown title"
        self.company = _clean(self.company) or "Unknown company"
        self.location = _clean(self.location) or "Unknown"
        self.description = _clean(self.description)
        self.apply_url = self.apply_url or self.url
        if not self.apply_email:
            found = EMAIL_RE.search(self.description)
            # Ignore obvious non-application addresses.
            if found and not re.search(r"(privacy|gdpr|dataskydd|noreply|no-reply)", found.group(0), re.I):
                self.apply_email = found.group(0)

    @property
    def id(self) -> str:
        key = self.url or f"{self.source}:{self.external_id}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    @property
    def search_text(self) -> str:
        return f"{self.title} {self.company} {self.location} {self.description}".lower()

    def summary_line(self) -> str:
        return f"{self.title} at {self.company} ({self.location})"


@dataclass
class Assessment:
    job_id: str
    score: int
    verdict: str                 # apply | maybe | skip
    reasoning: str = ""
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    posting_language: str = "en"
    model: str = "heuristic"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class Application:
    job_id: str
    status: str = "draft"        # draft | approved | sent | rejected | failed
    channel: str = "manual"      # email | manual | ats
    subject: str = ""
    cover_letter: str = ""
    recipient: str = ""
    notes: str = ""
    sent_at: Optional[str] = None
