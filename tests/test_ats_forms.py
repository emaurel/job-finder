"""Tests for ATS form understanding.

A bug here does not crash, it sends a company a confident lie about the candidate.
The Outlier case below is real: a form asked "have you worked with X? If yes,
give the email you signed up with" and an identity rule matching /e-?mail/
filled in his address, silently asserting a relationship that does not exist.

Run:  PYTHONPATH=src .venv/bin/python tests/test_ats_forms.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobfinder.config import profile  # noqa: E402
from jobfinder.delivery.ats_forms import (  # noqa: E402
    DEMOGRAPHIC, build_schema, deterministic, _scrub,
)

CV = "/tmp/cv.pdf"
# Read the expected values from whatever profile is loaded, so these tests pass
# on a fresh clone (which has only profile.example.yaml) as well as locally.
IDENT = profile()["identity"]
FIRST, _, LAST = IDENT["full_name"].partition(" ")


def ctrl(**kw):
    base = dict(jf=0, tag="input", type="text", name="", id="", required=False,
                label="", contexts=[], options=[], combobox=False, value="")
    base.update(kw)
    return base


def run() -> int:
    failures = 0

    def check(name: str, got, expected) -> None:
        nonlocal failures
        ok = got == expected
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:42} -> {got!r}"
              + ("" if ok else f"   expected {expected!r}"))

    # --- identity fields fill deterministically ---
    fields = build_schema([
        ctrl(jf=0, id="first_name", label="First Name*", required=True),
        ctrl(jf=1, id="last_name", label="Last Name*", required=True),
        ctrl(jf=2, id="email", label="Email*", required=True),
        ctrl(jf=3, id="phone", type="tel", label="Phone*", required=True),
        ctrl(jf=4, id="resume", type="file", label="Attach"),
    ])
    answers = deterministic(fields, "COVER", CV)
    check("first name", answers.get(0), FIRST)
    check("last name", answers.get(1), LAST)
    check("email label", answers.get(2), IDENT["email"])
    check("cv attaches", answers.get(4), {"file": CV})

    # --- the conditional-email trap ---
    trap = build_schema([ctrl(
        jf=0, id="q1", required=True,
        label="Have you previously worked on or are you currently registered "
              "with Outlier? If yes, please provide the email address you used "
              "to sign up.")])
    check("conditional email NOT auto-filled", 0 in deterministic(trap, "C", CV), False)

    long_q = build_schema([ctrl(
        jf=0, id="q2", required=True,
        label="If you have a current employer, what is your current company's "
              "legal name and country of registration?")])
    check("long company question left to LLM", 0 in deterministic(long_q, "C", CV), False)

    # --- demographic questions are declined, never guessed ---
    demo = build_schema([
        ctrl(jf=0, type="radio", name="g", label="Man", required=True,
             contexts=["What is your gender? Woman Man Prefer not to say"]),
        ctrl(jf=1, type="radio", name="g", label="Woman", required=True,
             contexts=["What is your gender? Woman Man Prefer not to say"]),
        ctrl(jf=2, type="radio", name="g", label="Prefer not to say", required=True,
             contexts=["What is your gender? Woman Man Prefer not to say"]),
    ])
    check("gender question detected", bool(DEMOGRAPHIC.search(demo[0].question)), True)
    check("gender declined", deterministic(demo, "C", CV).get(0), "Prefer not to say")

    # --- radio groups collapse into one question with its real text ---
    lever = build_schema([
        ctrl(jf=0, type="radio", name="cards[abc]", label="Yes", required=True,
             contexts=["Yes No N/A", "This role requires you to be based in "
                       "either London or Stockholm. Are you open to relocating? Yes No N/A"]),
        ctrl(jf=1, type="radio", name="cards[abc]", label="No", required=True,
             contexts=["Yes No N/A", "This role requires you to be based in "
                       "either London or Stockholm. Are you open to relocating? Yes No N/A"]),
        ctrl(jf=2, type="radio", name="cards[abc]", label="N/A", required=True,
             contexts=["Yes No N/A", "This role requires you to be based in "
                       "either London or Stockholm. Are you open to relocating? Yes No N/A"]),
    ])
    check("radio group collapses", len(lever), 1)
    check("group question found",
          lever[0].question.startswith("This role requires you to be based"), True)
    check("group options kept", lever[0].option_labels(), ["Yes", "No", "N/A"])

    # --- Greenhouse combobox pairs collapse, combobox variant wins ---
    paired = build_schema([
        ctrl(jf=0, id="country", label="Country*", required=True, combobox=True),
        ctrl(jf=1, id="", label="Country*", required=True, combobox=False),
    ])
    check("combobox pair dedupes", len(paired), 1)
    check("combobox variant kept", paired[0].combobox, True)

    # --- a selected combobox reports its container text, not el.value ---
    selected = build_schema([ctrl(
        jf=0, id="q3", label="What is the highest degree?", required=True,
        combobox=True, value="What is the highest degree? Master's")])
    check("combobox display value", selected[0].value, "Master's")

    # --- the no-em-dash rule reaches form answers too ---
    check("dashes scrubbed", _scrub("Yes — Sweden – no visa"),
          "Yes, Sweden, no visa")

    # --- field identity survives a re-render ---
    check("stable key", build_schema([ctrl(jf=7, id="email", label="Email*")])[0].key(),
          "text||email|Email*")

    print(f"\n{16 - failures}/16 passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
