"""Configuration: env vars and the master profile."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
_PROFILE = ROOT / "profile" / "profile.yaml"
_PROFILE_EXAMPLE = ROOT / "profile" / "profile.example.yaml"
# A fresh clone has no profile.yaml (it holds personal data and is gitignored),
# so fall back to the example: the project runs and the tests pass out of the box.
PROFILE_PATH = _PROFILE if _PROFILE.exists() else _PROFILE_EXAMPLE
DB_PATH = DATA_DIR / "jobs.db"

load_dotenv(ROOT / ".env")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def env_bool(key: str, default: bool) -> bool:
    raw = env(key)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int) -> int:
    raw = env(key)
    try:
        return int(raw)
    except ValueError:
        return default


@lru_cache(maxsize=1)
def profile() -> dict[str, Any]:
    with PROFILE_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# Models. Scoring runs on every job, so it uses the cheaper tier; drafting runs
# only on jobs that survived scoring and is worth the better model.
SCORING_MODEL = env("SCORING_MODEL", "claude-sonnet-5")
DRAFTING_MODEL = env("DRAFTING_MODEL", "claude-opus-5")

MAX_SENDS_PER_DAY = env_int("MAX_SENDS_PER_DAY", 10)
REQUIRE_APPROVAL = env_bool("REQUIRE_APPROVAL", True)
# Browser submission of ATS forms is off by default and separate from
# REQUIRE_APPROVAL: approving an application should not, on its own, cause a
# robot to click Submit on a company's website.
ATS_SUBMIT = env_bool("ATS_SUBMIT", False)
ATS_HEADLESS = env_bool("ATS_HEADLESS", True)

# Score thresholds, calibrated against real LLM output rather than guessed.
# An honest screen is harsh: on a typical day's pull the best genuine matches
# land in the 50s and there is a clear gap down to the high 30s. Retune these
# as profile/companies.yaml grows and the input pool improves.
SCORE_DRAFT_THRESHOLD = env_int("SCORE_DRAFT_THRESHOLD", 50)  # write a letter above this
SCORE_SHOW_THRESHOLD = env_int("SCORE_SHOW_THRESHOLD", 25)    # show in review UI above this


def has_anthropic_key() -> bool:
    return bool(env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
