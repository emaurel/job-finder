"""Find the real application form behind an aggregator listing.

RemoteOK, Jobicy and friends link to their own page, not to the employer's
form, and their outbound links often redirect in a loop. But most of those
employers still run their hiring on Greenhouse, Lever or Ashby, so the form is
reachable if we can guess the board.

Sticker Mule is the case this was written for: RemoteOK gave us a dead
redirect, while jobs.ashbyhq.com/stickermule was live the whole time.
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

import httpx2 as httpx

from .models import Job

log = logging.getLogger(__name__)

UA = {"User-Agent": "JobFinderBot/2.0 (+https://emaurel.github.io)"}

BOARDS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true", "jobs"),
    "lever": ("https://api.lever.co/v0/postings/{}?mode=json", None),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{}", "jobs"),
}

# company name -> (ats, slug) or None. Probing is the expensive part.
_board_cache: dict[str, tuple[str, str] | None] = {}


def slugs_for(company: str) -> list[str]:
    """Plausible board slugs for a company name, best guess first."""
    base = re.sub(r"\b(inc|ltd|llc|gmbh|ab|bv|sa|sas|oy|as|corp|co|group|labs?)\b", " ",
                  company.lower())
    base = re.sub(r"[^a-z0-9 ]", " ", base).strip()
    if not base:
        return []
    squashed = base.replace(" ", "")
    hyphened = base.replace(" ", "-")
    first = base.split()[0]
    out = [squashed, hyphened, first]
    seen, unique = set(), []
    for s in out:
        if s and s not in seen and len(s) > 2:
            seen.add(s)
            unique.append(s)
    return unique


def _board_jobs(ats: str, slug: str) -> list[dict] | None:
    url, key = BOARDS[ats]
    try:
        resp = httpx.get(url.format(slug), timeout=10, headers=UA)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if key is None:
            return data if isinstance(data, list) else None
        return data.get(key) if isinstance(data, dict) else None
    except Exception:
        return None


def find_board(company: str) -> tuple[str, str] | None:
    """Which ATS board this company uses, if we can find one."""
    if company in _board_cache:
        return _board_cache[company]
    result = None
    for slug in slugs_for(company):
        for ats in BOARDS:
            jobs = _board_jobs(ats, slug)
            if jobs:
                result = (ats, slug)
                break
        if result:
            break
    _board_cache[company] = result
    return result


def _title_of(ats: str, posting: dict) -> str:
    return posting.get("title") or posting.get("text") or ""


def _apply_url(ats: str, posting: dict) -> str:
    if ats == "greenhouse":
        return posting.get("absolute_url", "")
    if ats == "lever":
        return posting.get("applyUrl") or posting.get("hostedUrl", "")
    return posting.get("applyUrl") or posting.get("jobUrl", "")


def resolve(job: Job, threshold: float = 0.6) -> tuple[str, str] | None:
    """Return (ats, apply_url) if this listing's real form can be found.

    Matches on title similarity, so a board with one obvious counterpart wins
    and a board full of unrelated roles does not produce a wrong link.
    """
    board = find_board(job.company)
    if not board:
        return None
    ats, slug = board
    postings = _board_jobs(ats, slug) or []

    target = re.sub(r"[^a-z0-9 ]", " ", job.title.lower()).strip()
    best, best_score = None, 0.0
    for posting in postings:
        title = re.sub(r"[^a-z0-9 ]", " ", _title_of(ats, posting).lower()).strip()
        if not title:
            continue
        score = SequenceMatcher(None, target, title).ratio()
        if score > best_score:
            best, best_score = posting, score

    if best is None or best_score < threshold:
        log.debug("no confident match for %r on %s/%s (best %.2f)",
                  job.title, ats, slug, best_score)
        return None
    url = _apply_url(ats, best)
    if not url:
        return None
    if ats == "ashby" and not url.endswith("/application"):
        url = url.rstrip("/") + "/application"
    return ats, url
