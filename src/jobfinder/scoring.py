"""Score a job against the master profile.

Two paths. With an Anthropic key, Claude reads the posting against the full
profile and returns a structured judgement. Without one, a keyword heuristic
keeps the pipeline runnable - less accurate, but it never blocks the run.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import yaml

from . import config
from .models import Assessment, Job

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-100"},
        "verdict": {"type": "string", "enum": ["apply", "maybe", "skip"]},
        "reasoning": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "posting_language": {"type": "string", "enum": ["en", "fr", "sv", "de", "other"]},
    },
    "required": ["score", "verdict", "reasoning", "strengths", "gaps",
                 "red_flags", "posting_language"],
    "additionalProperties": False,
}

SYSTEM = """You screen job postings for one specific candidate. You are the
gatekeeper before their time gets spent, so be decisive and be honest.

Score 0-100 on how worth applying this posting is FOR THIS CANDIDATE:
  85-100  strong match, apply today
  65-84   good match, worth a tailored application
  40-64   plausible but compromised on something that matters
  0-39    not worth the time

Judge on fit, not on how prestigious the company sounds. A no-name company
building the right thing beats a famous one offering the wrong role.

Weigh these heavily:
  - Does the actual day-to-day work match what the candidate does well?
  - Can the candidate legally and practically do this job from Sweden as an EU
    citizen invoicing through a French micro-entreprise? Anything requiring
    relocation to a city other than Jonkoping, or work authorisation outside the
    EU, is a hard problem, not a detail.
  - Seniority. The candidate is early-career with deep project work. A staff or
    principal role is a waste of an application; a junior role is a waste of the
    candidate.
  - The posting's own red flags: vague client, unpaid, equity-only, body-shop
    agency listing, or a description so generic it could be any company.

Put anything that makes the application impossible or pointless in red_flags.
Put honest weaknesses in gaps; the cover letter will have to address them.
Report posting_language as the language the posting is WRITTEN in.

Never inflate a score to be encouraging. A wrong 'apply' costs more than a
wrong 'skip'."""


def _profile_brief() -> str:
    p = config.profile()
    brief = {
        "identity": {k: v for k, v in p["identity"].items() if k != "links"},
        "preferences": p["preferences"],
        "skills": p["skills"],
        "experience": p["experience"],
        "projects": [{k: v for k, v in proj.items() if k != "note_for_letters"}
                     for proj in p["projects"]],
        "education": p["education"],
    }
    return yaml.safe_dump(brief, allow_unicode=True, sort_keys=False)


def _job_brief(job: Job) -> str:
    return yaml.safe_dump({
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "remote": job.is_remote,
        "salary": job.salary or "not stated",
        "source": job.source,
        "posted": job.posted_at,
        "description": job.description[:6000],
    }, allow_unicode=True, sort_keys=False)


# --- Heuristic fallback -----------------------------------------------------

# The prose boost/penalise lists in profile.yaml are written for the LLM to read.
# The heuristic cannot interpret prose, so it gets its own explicit patterns.
# Keep these roughly in sync with profile.yaml by hand.
HEURISTIC_BOOST = {
    "llm": 8, "genai": 8, "generative ai": 8, "agentic": 8, "\bagent\b": 6,
    "\bmcp\b": 8, "\brag\b": 6, "prompt engineering": 6, "fine-tun": 4,
    "machine learning": 5, "pytorch": 4, "tensorflow": 3,
    "python": 4, "typescript": 4, "\brust\b": 6, "fastapi": 4, "react": 2, "docker": 2,
    "early.stage|seed|series a": 3, "open source": 2,
}
HEURISTIC_PENALTY = {
    "data (labell?ing|annotation)": 25,
    "\bcrypto|web3|blockchain|gambling|betting|casino\b": 30,
    "security clearance|\bsc cleared\b|ts/sci": 40,
    "equity only|unpaid|no salary": 40,
    "\bon.call rotation\b": 8,
    "staffing agency|body shop|our client is a": 12,
}


def _heuristic(job: Job, why: str = "no ANTHROPIC_API_KEY set") -> Assessment:
    text = job.search_text
    score = 45
    red: list[str] = []

    for pattern, weight in HEURISTIC_BOOST.items():
        if re.search(pattern, text):
            score += weight

    for pattern, weight in HEURISTIC_PENALTY.items():
        if re.search(pattern, text):
            score -= weight
            red.append(f"penalised: {pattern}")

    if job.is_remote:
        score += 8
    if re.search(r"\b(senior|sr\.?)\b", job.title, re.I):
        score -= 4   # reachable, but a stretch this early in a career

    # Content-free aggregator reposts: no company worth the name, or a title that
    # is mostly keyword soup. The LLM catches these properly; this is a crude proxy.
    if re.search(r"(jobleads|railway\.app|liveblog|dedyn\.io|vacancy)", job.url, re.I):
        score -= 25
        red.append("looks like an aggregator repost, not a direct employer listing")

    score = max(0, min(100, score))
    verdict = "apply" if score >= 70 else ("maybe" if score >= 45 else "skip")

    lang = "en"
    if re.search(r"\b(och|f\u00f6r|med|arbete|tj\u00e4nst|erfarenhet|vi s\u00f6ker)\b", text):
        lang = "sv"
    elif re.search(r"\b(nous|vous|votre|exp\u00e9rience|poste|recherche)\b", text):
        lang = "fr"

    return Assessment(
        job_id=job.id, score=score, verdict=verdict,
        reasoning=f"Keyword heuristic only ({why}). This cannot judge whether the "
                  f"role actually fits.",
        strengths=[], gaps=[], red_flags=red, posting_language=lang, model="heuristic",
    )


# --- LLM path ---------------------------------------------------------------

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _llm(job: Job) -> Assessment:
    client = _get_client()
    response = client.messages.create(
        model=config.SCORING_MODEL,
        max_tokens=8000,
        system=[{"type": "text", "text": SYSTEM + "\n\nCANDIDATE PROFILE:\n" + _profile_brief(),
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "Score this posting:\n\n" + _job_brief(job)}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        thinking={"type": "adaptive"},
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError("scoring response truncated; raise max_tokens")
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined to score this posting")
    text = next(b.text for b in response.content if b.type == "text")
    data: dict[str, Any] = json.loads(text)
    return Assessment(
        job_id=job.id,
        score=max(0, min(100, int(data["score"]))),
        verdict=data["verdict"],
        reasoning=data["reasoning"],
        strengths=data["strengths"],
        gaps=data["gaps"],
        red_flags=data["red_flags"],
        posting_language=data["posting_language"],
        model=config.SCORING_MODEL,
    )


def score_job(job: Job) -> Assessment:
    if not config.has_anthropic_key():
        return _heuristic(job)
    try:
        return _llm(job)
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM scoring failed for %s (%s); falling back to heuristic",
                    job.summary_line(), exc)
        return _heuristic(job, why=f"LLM call failed: {type(exc).__name__}")
