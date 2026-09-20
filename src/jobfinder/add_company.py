"""Add a company's ATS board to the watchlist.

Takes a company name or any URL from their careers page, works out which
platform they use, checks the board actually returns jobs, and writes it into
profile/companies.yaml.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx2 as httpx
import yaml

from .config import ROOT

COMPANIES = ROOT / "profile" / "companies.yaml"
UA = {"User-Agent": "JobFinderBot/2.0 (+https://emaurel.github.io)"}

URL_PATTERNS = [
    (re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([\w-]+)", re.I), "greenhouse"),
    (re.compile(r"greenhouse\.io/embed/job_board\?for=([\w-]+)", re.I), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([\w-]+)", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([\w-]+)", re.I), "ashby"),
]

PROBES = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{}/jobs", "jobs"),
    "lever": ("https://api.lever.co/v0/postings/{}?mode=json", None),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{}", "jobs"),
}


def count_jobs(ats: str, slug: str) -> int:
    """How many openings that board returns. 0 means wrong slug or wrong platform."""
    url, key = PROBES[ats]
    try:
        resp = httpx.get(url.format(slug), timeout=12, headers=UA)
        if resp.status_code != 200:
            return 0
        data = resp.json()
        if key is None:
            return len(data) if isinstance(data, list) else 0
        return len(data.get(key, [])) if isinstance(data, dict) else 0
    except Exception:
        return 0


def resolve(target: str) -> list[tuple[str, str, int]]:
    """Return every (ats, slug, job_count) that works for this input."""
    for pattern, ats in URL_PATTERNS:
        match = pattern.search(target)
        if match:
            slug = match.group(1)
            n = count_jobs(ats, slug)
            return [(ats, slug, n)] if n else []

    slug = re.sub(r"[^a-z0-9-]", "", target.strip().lower().replace(" ", ""))
    hits = [(ats, slug, count_jobs(ats, slug)) for ats in PROBES]
    return [h for h in hits if h[2] > 0]


def add(ats: str, slug: str) -> str:
    text = COMPANIES.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if slug in (data.get(ats) or []):
        return f"{slug} is already in the {ats} list"

    # Append under the right key, preserving the file's comments.
    lines = text.rstrip("\n").split("\n")
    insert_at, in_section = None, False
    for i, line in enumerate(lines):
        if line.startswith(f"{ats}:"):
            in_section = True
            insert_at = i + 1
            continue
        if in_section:
            if line.startswith("  - ") or line.strip().startswith("#") or not line.strip():
                insert_at = i + 1
            elif not line.startswith(" "):
                break
    if insert_at is None:
        lines += [f"{ats}:", f"  - {slug}"]
    else:
        while insert_at > 0 and not lines[insert_at - 1].strip().startswith("- "):
            insert_at -= 1
        lines.insert(insert_at, f"  - {slug}")
    COMPANIES.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"added {slug} to {ats}"
