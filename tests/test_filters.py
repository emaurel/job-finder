"""Regression tests for the free-rejection layer.

These matter more than they look: every job that slips through here costs an
LLM call, and every job wrongly rejected here is never seen again.

Run:  PYTHONPATH=src .venv/bin/python tests/test_filters.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobfinder.config import profile  # noqa: E402
from jobfinder.filters import reject_reason  # noqa: E402

# The one city the profile says is commutable. Read it rather than hardcode it,
# so these pass on a fresh clone using profile.example.yaml.
HOME_CITY = profile()["preferences"]["hard_filters"]["reject_onsite_outside"][0]
from jobfinder.models import Job  # noqa: E402
from jobfinder.pipeline import dedupe  # noqa: E402


def mk(**kw) -> Job:
    base = dict(
        source="test", external_id=kw.get("title", "x"), title="AI Engineer",
        company="X", location="Remote", url="https://e.com/" + str(kw.pop("n", 1)),
        description="We build LLM systems in Python and FastAPI. " * 6,
    )
    base.update(kw)
    base.setdefault("external_id", base["title"])
    return Job(**base)


CASES: list[tuple[str, Job, str]] = [
    # --- survives ---
    ("clean remote",        mk(is_remote=True), ""),
    ("3 years is fine",     mk(description="Need 3+ years of experience in ML. " * 6), ""),
    ("home city onsite",    mk(location=f"{HOME_CITY}, Sweden"), ""),
    # Remote is not enough: a Swedish-language posting means a Swedish-speaking
    # workplace. Only an explicit English-working-language statement rescues it.
    ("swedish remote, no english", mk(location="Stockholm, Sweden",
                                      description="Bra roll. Distansarbete är möjligt "
                                                  "för dig som vill arbeta med oss. " * 6),
     "posting_language:sv"),
    ("swedish but english workplace", mk(location="Stockholm, Sweden",
                                         description="Bra roll. Distansarbete är möjligt "
                                                     "för dig som vill arbeta med oss. " * 6
                                                     + "English is our working language."),
     ""),
    ("german posting", mk(description="Wir suchen einen Entwickler für unser Team. "
                                      "Sie haben Erfahrung mit Python und wollen bei "
                                      "uns im Büro arbeiten. " * 5, is_remote=True),
     "posting_language:de"),
    ("french posting is fine", mk(description="Nous recherchons un développeur pour "
                                              "notre équipe. Vous avez une expérience "
                                              "avec Python dans votre poste. " * 5,
                                  is_remote=True), ""),
    ("remote canada AND uk", mk(location="Remote, Canada; Remote, United Kingdom", is_remote=True), ""),
    ("bare engineer title", mk(title="Senior Engineer II", is_remote=True), ""),
    ("swedish utvecklare",  mk(title="Systemutvecklare Python", location=HOME_CITY), ""),
    ("backend ai platform", mk(title="Backend Engineer, AI Platform", is_remote=True), ""),

    # --- rejected on role ---
    ("sales engineer",      mk(title="Senior Sales Engineer", is_remote=True), "irrelevant_role"),
    ("account executive",   mk(title="Strategic Account Executive", is_remote=True), "irrelevant_role"),
    ("solutions architect", mk(title="Solutions Architect AI/ML", is_remote=True), "irrelevant_role"),
    ("developer advocate",  mk(title="Developer Advocate", is_remote=True), "irrelevant_role"),
    ("business developer",  mk(title="Business Developer (x/f/m)", is_remote=True), "irrelevant_role"),
    ("legal engineer",      mk(title="Legal Engineer", is_remote=True), "irrelevant_role"),
    ("recruiter",           mk(title="Technical Recruiter", is_remote=True), "irrelevant_role"),

    # --- rejected on seniority ---
    ("staff backend",       mk(title="Staff Backend Engineer", is_remote=True), "too_senior"),
    ("staff core platform", mk(title="Staff Core Platform Engineer", is_remote=True), "too_senior"),
    ("engineering manager", mk(title="Engineering Manager, Platform", is_remote=True), "too_senior"),
    ("internship",          mk(title="Software Engineer Internship", is_remote=True), "too_junior"),

    # --- rejected on geography ---
    ("stockholm onsite",    mk(location="Stockholm, Sweden"), "onsite_elsewhere:Stockholm, Sweden"),
    ("london onsite",       mk(location="London"), "onsite_elsewhere:London"),
    ("hybrid is onsite",    mk(title="Senior Engineer Stockholm - Hybrid", location="Stockholm"),
                            "onsite_elsewhere:Stockholm"),
    ("remote negated",      mk(location="Berlin", description="This is not a remote role. " * 8),
                            "onsite_elsewhere:Berlin"),
    ("remote but bangalore", mk(location="Remote, Bangalore", is_remote=True), "remote_locked_to:Bangalore"),
    ("ats remote flag + NY", mk(location="New York, New York", is_remote=True), "remote_locked_to:New York"),

    # --- rejected on content ---
    ("us only",             mk(description="Great LLM role. US citizens only. " * 8), "us_only"),
    ("7 years required",    mk(description="Need 7+ years of experience in ML. " * 6), "experience:7y"),
    ("clearance",           mk(description="Requires an active security clearance. " * 8), "security_clearance"),
    ("thin description",    mk(description="Short."), "description_too_thin"),
]


def main() -> int:
    failures = 0
    for name, job, expected in CASES:
        got = reject_reason(job)
        ok = got == expected
        failures += not ok
        mark = "PASS" if ok else "FAIL"
        extra = "" if ok else f"   expected {expected!r}"
        print(f"{mark}  {name:22} -> {got!r}{extra}")

    # "20 years of combined team experience" is boilerplate, not a requirement.
    from jobfinder.filters import max_experience_required
    boilerplate = max_experience_required(mk(description="Our team has 20 years of experience. " * 6))
    ok = boilerplate == 0
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {'20y boilerplate ignored':22} -> {boilerplate}")

    # Same role posted per country collapses to the EU-friendliest variant.
    jobs = [mk(title="Senior Backend Engineer", company="Gitlab", location="Remote, Canada", n=1),
            mk(title="Senior Backend Engineer", company="Gitlab", location="Remote, United Kingdom", n=2),
            mk(title="AI Engineer", company="Other", location="Remote", n=3)]
    deduped = dedupe(jobs)
    ok = len(deduped) == 2 and any("United Kingdom" in j.location for j in deduped)
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {'dedupe keeps EU variant':22} -> {[j.location for j in deduped]}")

    total = len(CASES) + 2
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
