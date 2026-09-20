"""Cheap deterministic rejection, before anything costs an API call.

Ported from the original n8n `Process & Format Email` code node, with the
hardcoded values lifted into profile.yaml.
"""
from __future__ import annotations

import re
import unicodedata

from .config import profile
from .models import Job

US_ONLY_PATTERNS = [
    "us only", "u.s. only", "united states only", "usa only",
    "must be based in the us", "must reside in the us", "must be located in the us",
    "authorized to work in the us", "authorized to work in the united states",
    "must be a us citizen", "us citizens only", "only us residents",
    "us-based only", "us based only", "only accepting applicants from the us",
    "work authorization in the united states", "must be located in the united states",
]

# Countries/regions that mean "you cannot do this from Sweden".
ONSITE_ELSEWHERE = [
    "stockholm", "malmo", "goteborg", "gothenburg", "linkoping", "uppsala",
    "vasteras", "orebro", "helsingborg", "norrkoping", "lund", "umea", "lulea",
]

SWEDEN_MARKERS = ["sweden", "sverige"] + ONSITE_ELSEWHERE

CLEARANCE = re.compile(
    r"(security clearance|nato secret|sc cleared|dv cleared|ssbi|ts/sci|"
    r"säkerhetsprövning|sakerhetsprovning)", re.I)

EXPERIENCE_PATTERNS = [
    r"\b(\d{1,2})\s*\+?\s*years?\s*(?:of\s*)?(?:professional\s*|relevant\s*|hands[- ]on\s*)?(?:experience|exp)\b",
    r"minimum\s+(?:of\s+)?(\d{1,2})\s*years?",
    r"at\s+least\s+(\d{1,2})\s*years?",
    r"\b(\d{1,2})\s*\+\s*yrs?\b",
    r"\b(\d{1,2})\s*-\s*\d{1,2}\s*years?\s*(?:of\s*)?experience",
]


def _fold(text: str) -> str:
    """Lowercase and strip accents, so 'Jonkoping' matches 'Jönköping'."""
    norm = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in norm if not unicodedata.combining(c))


def _prefs() -> dict:
    return profile().get("preferences", {}).get("hard_filters", {})


def is_us_only(job: Job) -> bool:
    text = _fold(job.search_text)
    return any(p in text for p in US_ONLY_PATTERNS)


def is_onsite_elsewhere(job: Job) -> bool:
    """On-site in a Swedish city that is not Jonkoping, or on-site abroad."""
    if job.is_remote:
        return False
    loc = _fold(job.location)
    allowed = [_fold(c) for c in _prefs().get("reject_onsite_outside", ["Jonkoping"])]
    if any(a in loc for a in allowed):
        return False
    text = _fold(job.search_text)
    if any(kw in text for kw in ("remote", "distans", "distansarbete", "work from home", "teletravail")):
        return False
    # Only veto when we are confident it is a Swedish posting pinned elsewhere.
    if any(marker in loc for marker in SWEDEN_MARKERS):
        return True
    return False


def max_experience_required(job: Job) -> int:
    """Highest 'N years experience' figure the posting demands. 0 if none found."""
    text = job.search_text
    years = []
    for pattern in EXPERIENCE_PATTERNS:
        for m in re.finditer(pattern, text, re.I):
            try:
                years.append(int(m.group(1)))
            except (ValueError, IndexError):
                continue
    # 10+ is almost always "10 years of combined team experience" boilerplate.
    return max((y for y in years if y <= 15), default=0)


def requires_too_much_experience(job: Job) -> bool:
    cap = _prefs().get("max_years_experience_required", 4)
    return max_experience_required(job) > cap


def requires_clearance(job: Job) -> bool:
    return bool(CLEARANCE.search(job.search_text))




# --- Role relevance ---------------------------------------------------------
# The ATS boards return every opening a company has, sales and legal included.
# This gate is keyword-cheap on purpose: it runs on thousands of postings so the
# LLM only ever sees plausible ones.

ROLE_INCLUDE = re.compile(r"""
    \b(
      software\s+engineer | backend | back[- ]end | full[- ]?stack | frontend | front[- ]end
    | ai\s+engineer | ml\s+engineer | machine\s+learning | deep\s+learning
    | llm | genai | generative\s+ai | nlp | research\s+engineer | applied\s+scientist
    | data\s+engineer | platform\s+engineer | infrastructure\s+engineer
    | developer | utvecklare | systemutvecklare | programmer | programmeur
    | d[ée]veloppeur | ingenieur\s+logiciel | ing[ée]nieur
    | python | rust | typescript | golang
    # Bare 'engineer' last: ROLE_EXCLUDE runs first, so sales and solutions
    # engineers are already gone by the time this can match.
    | engineer
    )\b
    # Swedish compounds words, so \b never matches inside them:
    # "Fullstackutvecklare" contains "utvecklare" with no boundary in front.
    # These alternatives deliberately have no leading \b.
    | \w*utvecklare | \w*utveckling | \w*ingenj[oö]r | programmerare
    | \b(ai|ml|it|data|system|l[oö]snings|mjukvaru|molnn?)[- ]?arkitekt
    """, re.I | re.X)

# Functions that are not engineering, or are engineering-adjacent pre-sales.
ROLE_EXCLUDE = re.compile(r"""
    \b(
      sales | account\s+(executive|manager|director) | business\s+develop\w*
    | solutions?\s+(engineer|architect|consultant) | pre[- ]?sales | field\s+engineer
    | customer\s+(success|support) | technical\s+support | support\s+engineer
    | marketing | brand | communications | content\s+(writer|strateg)
    | recruit | talent | people\s+(ops|partner) | human\s+resources | \bhr\b
    | legal | counsel | compliance\s+officer | paralegal
    | finance | accounting | controller | payroll | tax
    | office\s+manager | executive\s+assistant | receptionist
    | designer | ux\s+research | product\s+design | graphic
    | community\s+manager | partnerships? | reseller | channel
    | developer\s+(advocate|relations) | devrel | evangelist
    | teacher | l[äa]rare | instructor | curriculum
    # Swedish equivalents of the exclusions above.
    | s[äa]ljare | rekryterare | projektledare | produktchef | fors[äa]ljning
    | kundtj[äa]nst | marknadsf[oö]ring | ekonom | redovisning | l[oö]nespecialist
    | l[oö]sningsarkitekt
    )\b""", re.I | re.X)

# Seniority that makes an application a waste of time in either direction.
TOO_SENIOR = re.compile(r"""
    \b(
      staff | principal\s+engineer | distinguished\s+engineer
    | engineering\s+manager | director | vice\s+president | \bvp\b | head\s+of
    | chief\s+\w+\s+officer | \bcto\b | \bceo\b
    )\b""", re.I | re.X)

TOO_JUNIOR = re.compile(r"\b(intern(ship)?|praktik|exjobb|thesis|trainee|apprentice|stage)\b", re.I)


def is_irrelevant_role(job: Job) -> bool:
    title = job.title
    if ROLE_EXCLUDE.search(title):
        return True
    return not ROLE_INCLUDE.search(title)


# --- Location reachability --------------------------------------------------
# Edgar works remote, or on-site in Jonkoping. Nothing else is reachable.

REMOTE_HINT = re.compile(
    r"\b(remote|distans|distansarbete|work from home|t[ée]l[ée]travail|anywhere)\b", re.I)

# A "remote" job can still be closed to him if it is region-locked.
REMOTE_OK_REGION = re.compile(
    r"\b(europe|european|emea|eu\b|worldwide|global|anywhere|nordic|sweden|sverige|"
    r"france|germany|netherlands|spain|portugal|poland|ireland|uk|united kingdom)\b", re.I)

REMOTE_BLOCKED_REGION = re.compile(
    r"\b(united states|usa|u\.s\.|us[- ]based|canada|latam|latin america|brazil|mexico|"
    r"apac|asia|india|japan|china|singapore|australia|new zealand|israel|"
    r"philippines|indonesia|korea|taiwan|africa|dubai|uae|argentina|colombia|chile|"
    # Cities, because an ATS "isRemote" flag is often set on a city-pinned role.
    r"san francisco|new york|seattle|austin|boston|chicago|los angeles|denver|atlanta|"
    r"miami|washington|palo alto|mountain view|toronto|vancouver|montreal|"
    r"bengaluru|bangalore|hyderabad|mumbai|delhi|pune|chennai|"
    r"tokyo|osaka|sydney|melbourne|auckland|singapore|hong kong|seoul|taipei|"
    r"tel aviv|sao paulo|s\u00e3o paulo|buenos aires|rosario|bogota|santiago|"
    r"mexico city|cairo|nairobi|lagos|johannesburg)\b", re.I)


# A bare "remote" in a description means nothing - half of them say "this is not
# a remote role". Only an explicit policy statement counts.
REMOTE_POLICY = re.compile(
    r"(fully[- ]remote|100%\s*remote|remote[- ]first|remote[- ]friendly|"
    r"work from anywhere|distansarbete|helt p[aa]\s*distans|arbeta p[aa] distans|"
    r"t[ée]l[ée]travail)", re.I)

REMOTE_NEGATED = re.compile(
    r"(not a remote|no remote|remote is not|onsite only|on[- ]site only|"
    r"ingen distans|pas de t[ée]l[ée]travail)", re.I)


def looks_remote(job: Job) -> bool:
    if job.is_remote:
        return True
    if REMOTE_HINT.search(job.location) or REMOTE_HINT.search(job.title):
        return True
    if REMOTE_NEGATED.search(job.description):
        return False
    return bool(REMOTE_POLICY.search(job.description))


def is_unreachable(job: Job) -> str:
    """Empty string if Edgar could actually hold this job from Jonkoping.

    The rule is simply his stated preference: remote, or on-site in Jonkoping.
    On-site in Stockholm or London is as unreachable as on-site in Tokyo, so
    geography is an allowlist and 'hybrid' counts as on-site.
    """
    allowed = [_fold(c) for c in _prefs().get("reject_onsite_outside", ["Jonkoping"])]
    loc_folded = _fold(job.location)

    if any(a in loc_folded for a in allowed):
        return ""

    if not looks_remote(job):
        return f"onsite_elsewhere:{job.location[:40]}"

    # It claims remote. It can still be pinned to a region he cannot work from.
    blocked = REMOTE_BLOCKED_REGION.search(job.location)
    if blocked and not REMOTE_OK_REGION.search(job.location):
        return f"remote_locked_to:{blocked.group(0)}"
    return ""


def reject_reason(job: Job) -> str:
    """Empty string means the job survives to LLM scoring.

    Ordered cheapest-and-most-selective first: role relevance kills the bulk of
    an ATS board in one regex before anything more expensive runs.
    """
    # Seniority first: "Engineering Manager" is a seniority rejection, and
    # labelling it "irrelevant_role" would make the funnel stats lie.
    if TOO_SENIOR.search(job.title):
        return "too_senior"
    if TOO_JUNIOR.search(job.title):
        return "too_junior"
    if is_irrelevant_role(job):
        return "irrelevant_role"
    unreachable = is_unreachable(job)
    if unreachable:
        return unreachable
    if _prefs().get("reject_if_us_only", True) and is_us_only(job):
        return "us_only"
    if requires_too_much_experience(job):
        return f"experience:{max_experience_required(job)}y"
    if requires_clearance(job):
        return "security_clearance"
    language = wrong_language(job)
    if language:
        return language
    if len(job.description) < 80:
        return "description_too_thin"
    return ""


# --- Working language -------------------------------------------------------
# A posting written in Swedish means a Swedish-speaking workplace. Edgar is
# learning the language but cannot work in it, so these are a hard no unless
# the posting says outright that English is the working language.

STOPWORDS = {
    "en": {"the", "and", "with", "for", "you", "your", "we", "our", "will", "have",
           "this", "that", "from", "are", "work", "team", "experience"},
    "fr": {"et", "le", "la", "les", "des", "une", "vous", "nous", "pour", "avec",
           "votre", "notre", "dans", "sur", "est", "qui", "poste", "expérience"},
    "sv": {"och", "att", "för", "med", "som", "vi", "du", "din", "vår", "är",
           "har", "till", "på", "av", "inom", "arbete", "erfarenhet", "söker",
           "tjänsten", "dig", "ett", "eller"},
    "de": {"und", "der", "die", "das", "mit", "für", "sie", "wir", "ist", "haben",
           "eine", "einen", "bei", "von", "zu", "im", "auf", "als", "erfahrung"},
    "nl": {"en", "de", "het", "een", "van", "voor", "met", "je", "wij", "zijn",
           "aan", "op", "bij", "die", "ervaring"},
    "es": {"y", "el", "la", "los", "las", "una", "para", "con", "que", "en",
           "por", "del", "experiencia", "trabajo"},
}

ENGLISH_WORKPLACE = re.compile(
    r"(english is (our|the) (working|company|official) language|"
    r"we work in english|working language is english|"
    r"english[- ]speaking (team|workplace|environment)|"
    r"all (our )?communication (is )?in english|"
    r"no swedish (is )?required|swedish is not required|"
    r"english only|fluent english is (all|the only))", re.I)


def detect_language(text: str) -> str:
    """Rough language guess from stopword hits. Good enough for a job posting."""
    words = re.findall(r"[a-zà-öø-ÿ]+", text.lower())[:400]
    if len(words) < 25:
        return "unknown"
    counts = {lang: sum(1 for w in words if w in bag) for lang, bag in STOPWORDS.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] >= 5 else "unknown"


def wrong_language(job: Job) -> str:
    """Empty string if Edgar could actually work here."""
    prefs = _prefs()
    if not prefs.get("reject_postings_not_in_working_language", True):
        return ""
    allowed = {lang.lower()[:2] for lang in prefs.get("working_languages", ["English", "French"])}
    allowed = {"en" if a == "en" else "fr" if a == "fr" else a for a in allowed}

    language = detect_language(job.description)
    if language in {"unknown"} or language in allowed:
        return ""
    if ENGLISH_WORKPLACE.search(job.description):
        return ""
    return f"posting_language:{language}"
