"""Read an ATS application form into a schema, and decide what to put in it.

Kept separate from the browser driver so the hard part - mapping a stranger's
form fields onto Edgar's profile - is testable without launching Chromium.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from .. import config
from ..models import Job

log = logging.getLogger(__name__)

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Tag every control in the page, then report it. Tagging gives us a selector
# that cannot go stale between reading the form and filling it.
EXTRACT_JS = r"""
() => {
  // "Select...", "Choose" and friends are placeholders, not content.
  const GENERIC = /^(select\.{0,3}|choose\.{0,3}|--.*--|please select.*|search.*)$/i;
  const useful = t => {
    t = (t || '').trim().replace(/\s+/g, ' ');
    return (t && t.length > 1 && !GENERIC.test(t)) ? t : '';
  };

  const visible = el =>
    el.type === 'file' ||
    el.offsetParent !== null ||
    el.getBoundingClientRect().height > 0;

  const controls = [...document.querySelectorAll('input, select, textarea')]
    .filter(el => el.type !== 'hidden' && visible(el));

  controls.forEach((el, i) => el.setAttribute('data-jf', String(i)));

  const ownLabel = el => {
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      const t = useful(l && l.innerText);
      if (t) return t;
    }
    const anc = el.closest('label');
    const ancT = useful(anc && anc.innerText);
    if (ancT) return ancT;

    const aria = useful(el.getAttribute('aria-label'));
    if (aria) return aria;

    // Greenhouse comboboxes put the question in a sibling ABOVE the wrapper.
    let n = el;
    for (let d = 0; d < 4 && n; d++, n = n.parentElement) {
      const prev = useful(n.previousElementSibling && n.previousElementSibling.innerText);
      if (prev && prev.length < 200) return prev;
    }

    return useful(el.placeholder);
  };

  // Several ancestor texts, innermost first. A radio group's own wrapper often
  // holds only the option labels; the question lives a level or two above it.
  const contexts = el => {
    const out = [];
    let n = el.parentElement;
    for (let d = 0; d < 6 && n; d++, n = n.parentElement) {
      const t = (n.innerText || '').trim().replace(/\s+/g, ' ');
      if (t.length > 3 && t.length < 600 && !out.includes(t)) out.push(t);
    }
    return out;
  };

  const isCombo = el =>
    el.getAttribute('role') === 'combobox' ||
    !!el.getAttribute('aria-autocomplete') ||
    el.getAttribute('aria-haspopup') === 'listbox' ||
    !!el.getAttribute('aria-controls');

  return controls.map((el, i) => ({
    jf: i,
    tag: el.tagName.toLowerCase(),
    type: (el.type || '').toLowerCase(),
    combobox: isCombo(el),
    value: (() => {
      if (el.type === 'checkbox' || el.type === 'radio') return el.checked ? 'CHECKED' : '';
      if (el.value) return el.value;
      // Greenhouse-style comboboxes clear the input on select and render the
      // chosen label as text in an ancestor, so el.value lies here.
      if (isCombo(el)) {
        let n = el.parentElement;
        for (let d = 0; d < 3 && n; d++, n = n.parentElement) {
          const t = useful(n.innerText);
          if (t && t.length < 120) return t;
        }
      }
      return '';
    })(),
    name: el.name || '',
    id: el.id || '',
    required: !!(el.required || el.getAttribute('aria-required') === 'true'),
    label: ownLabel(el).slice(0, 200),
    contexts: contexts(el),
    options: el.tagName === 'SELECT'
      ? [...el.options].map(o => o.label || o.text).filter(Boolean).slice(0, 200)
      : [],
  }));
}
"""


@dataclass
class Field:
    """One logical question, which may be several DOM controls (a radio group)."""

    kind: str                       # text | textarea | select | file | radio | checkbox
    question: str
    required: bool
    jf: int | None = None                       # single-control fields
    options: list[dict[str, Any]] = field(default_factory=list)  # [{label, jf}]
    select_options: list[str] = field(default_factory=list)
    name: str = ""
    element_id: str = ""
    combobox: bool = False
    value: str = ""

    def option_labels(self) -> list[str]:
        return self.select_options or [o["label"] for o in self.options]

    def key(self) -> str:
        """Identity that survives a React re-render, unlike the data-jf index."""
        return f"{self.kind}|{self.name}|{self.element_id}|{(self.question or '')[:70]}"


def _display_value(value: str, question: str) -> str:
    """A combobox reports its container text, which can include the question."""
    value = (value or "").strip()
    if question and question in value:
        value = value.replace(question, " ")
    return re.sub(r"\s+", " ", value).strip(" *\u2731")


REQUIRED_MARK = re.compile(r"[*\u2731]\s*$|[*\u2731]\s*\n")


def _looks_required(question: str, flagged: bool) -> bool:
    """Ashby and Lever mark required fields with a visual asterisk and set no
    aria-required, so a DOM-only check silently lets empty required fields
    through verification."""
    return bool(flagged or REQUIRED_MARK.search(question or ""))


def _group_key(raw: dict) -> str:
    if raw["name"]:
        return "n:" + raw["name"]
    if raw["id"] and UUID_RE.match(raw["id"]):
        return "u:" + UUID_RE.match(raw["id"]).group(0)
    return "j:" + str(raw["jf"])


def _strip_options(text: str, option_labels: list[str]) -> str:
    for label in sorted(option_labels, key=len, reverse=True):
        if label:
            text = text.replace(label, " ")
    return re.sub(r"\s+", " ", text).strip(" *✱\n\t,;:")


def _clean_question(contexts: list[str], option_labels: list[str]) -> str:
    """Find the ancestor text that is actually the question.

    Walking outward from the control, the innermost wrapper usually holds
    nothing but the option labels. Keep going until something meaningful
    survives having those labels removed.
    """
    for text in contexts or []:
        remainder = _strip_options(text, option_labels)
        if len(remainder) >= 8:
            return remainder[:220]
    return ""


def build_schema(raw_controls: list[dict]) -> list[Field]:
    """Collapse raw DOM controls into logical questions."""
    fields: list[Field] = []
    groups: dict[str, list[dict]] = {}

    for raw in raw_controls:
        if raw["type"] in {"radio", "checkbox"}:
            groups.setdefault(_group_key(raw), []).append(raw)
            continue
        kind = {"file": "file", "select-one": "select", "select-multiple": "select"}.get(
            raw["type"], "textarea" if raw["tag"] == "textarea" else
            "select" if raw["tag"] == "select" else "text")
        question = raw["label"] or _clean_question(raw.get("contexts", []), [])
        fields.append(Field(
            kind=kind, question=question,
            required=_looks_required(question, raw["required"]), jf=raw["jf"],
            select_options=raw["options"], name=raw["name"], element_id=raw["id"],
            combobox=raw.get("combobox", False),
            value=_display_value(raw.get("value", ""), question)))

    # Greenhouse renders each combobox as TWO inputs with the same question:
    # a visible search box and a hidden value holder. Keep the combobox only.
    deduped: list[Field] = []
    seen: dict[str, int] = {}
    for f in fields:
        q = (f.question or "").strip().lower()
        if q and q in seen:
            kept = deduped[seen[q]]
            if f.combobox and not kept.combobox:
                deduped[seen[q]] = f
            continue
        if q:
            seen[q] = len(deduped)
        deduped.append(f)
    fields = deduped

    for members in groups.values():
        option_labels = [m["label"] for m in members]
        question = (_clean_question(members[0].get("contexts", []), option_labels)
                    or members[0]["label"])
        fields.append(Field(
            kind=members[0]["type"],
            question=question,
            required=_looks_required(question, any(m["required"] for m in members)),
            options=[{"label": m["label"], "jf": m["jf"]} for m in members],
            name=members[0]["name"], element_id=members[0]["id"],
            value="CHECKED" if any(m.get("value") == "CHECKED" for m in members) else ""))

    return fields


# --- Deterministic answers --------------------------------------------------
# Identity facts should never be left to a model. Only judgement calls are.

DEMOGRAPHIC = re.compile(
    r"(gender|\bsex\b|race|ethnic|hispanic|latino|veteran|disabilit|"
    r"sexual orientation|transgender|pronoun|age range|date of birth)", re.I)

DECLINE = re.compile(
    r"(prefer not|decline|don'?t wish|do not wish|not to (say|answer|disclose)|"
    r"i don'?t want to answer)", re.I)


def _identity() -> dict[str, str]:
    p = config.profile()
    ident = p["identity"]
    links = ident.get("links", {})
    first, _, last = ident["full_name"].partition(" ")
    rate = p["preferences"]["rate"]
    return {
        "first": first, "last": last, "full": ident["full_name"],
        "email": ident["email"], "phone": ident["phone"],
        "city": ident["location"], "country": "Sweden",
        "linkedin": links.get("linkedin", ""), "github": links.get("github", ""),
        "portfolio": links.get("portfolio", ""),
        "company": "Freelance (auto-entreprise)",
        "salary": f"EUR {rate['freelance_day_eur']}/day (freelance) or open on salaried",
        "availability": p["preferences"]["availability"],
    }


# Identity rules are for form labels ("Email*"), never for prose questions.
IDENTITY_LABEL_MAX = 45

# Ordered: the first pattern that matches a question wins.
RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"first\s*(and last\s*)?name", re.I), "MATCH_FIRST"),
    (re.compile(r"\b(last|family|sur)\s*name", re.I), "last"),
    (re.compile(r"\b(full|your)\s*name\b|^name$", re.I), "full"),
    (re.compile(r"e-?mail", re.I), "email"),
    (re.compile(r"phone|mobile|telephone", re.I), "phone"),
    (re.compile(r"linked\s*in", re.I), "linkedin"),
    (re.compile(r"github", re.I), "github"),
    (re.compile(r"portfolio|personal (web)?site|website", re.I), "portfolio"),
    (re.compile(r"current (company|employer)|\borg\b", re.I), "company"),
    (re.compile(r"country", re.I), "country"),
    (re.compile(r"location|city|where are you (currently )?based|based\b", re.I), "city"),
    (re.compile(r"salary|compensation expectation|expected (yearly|annual)|rate", re.I), "salary"),
    (re.compile(r"notice period|when can you start|availability|start date", re.I), "availability"),
]


def deterministic(fields: list[Field], cover_letter: str, cv_path: str) -> dict[int, Any]:
    """Answers we are certain about. Keyed by the Field's index in `fields`."""
    ident = _identity()
    answers: dict[int, Any] = {}

    for idx, f in enumerate(fields):
        q = f.question or f.name

        if f.kind == "file":
            if re.search(r"cover\s*letter", q, re.I):
                continue          # handled as text where possible
            if re.search(r"resume|\bcv\b|attach", q, re.I) or "resume" in (f.name + f.element_id).lower():
                answers[idx] = {"file": cv_path}
            continue

        if f.kind == "textarea" and re.search(r"cover\s*letter|why|motivat|tell us", q, re.I):
            answers[idx] = cover_letter
            continue

        # Demographic questions: decline where we can, never guess.
        if DEMOGRAPHIC.search(q):
            if f.required:
                for opt in f.option_labels():
                    if DECLINE.search(opt):
                        answers[idx] = opt
                        break
            continue

        if f.kind in {"text", "select"}:
            # Identity rules may only match a short, label-like question.
            # "Email*" is Edgar's address. "If yes, please provide the email
            # address you used to sign up" is a question about a relationship
            # he does not have, and answering it asserts that he does.
            if len(q) > IDENTITY_LABEL_MAX or "?" in q:
                continue
            for pattern, key in RULES:
                if pattern.search(q):
                    value = ident["first"] if key == "MATCH_FIRST" and "last" not in q.lower() \
                        else (ident["full"] if key == "MATCH_FIRST" else ident[key])
                    if f.kind == "select":
                        match = next((o for o in f.select_options
                                      if value.lower() in o.lower() or o.lower() in value.lower()), None)
                        if match:
                            answers[idx] = match
                    else:
                        answers[idx] = value
                    break

    return answers


# --- LLM answers for everything else ----------------------------------------

SYSTEM = """You are filling in a job application form on behalf of one candidate.
Everything you write goes to a real employer under the candidate's name.

Rules:
  - Answer ONLY from the candidate profile you are given. Never invent an
    employer, a date, a number, a degree, or a skill.
  - If the profile does not support an answer, return null for that field. A
    null is safe; an invented answer is not. Do not guess to be helpful.
  - For a field with options, return EXACTLY one of the option strings, copied
    character for character. If none is truthful, return null.
  - For checkbox groups, return a list of the option strings that are true.
  - If a required question offers "None", "No experience" or an equivalent and
    that is the truthful answer, SELECT IT. Leaving a required field blank
    because the honest answer is "none" blocks the whole application for no
    reason. Blank is for when no option is truthful, not for when "none" is.
  - Keep free-text answers short and concrete, two or three sentences at most,
    unless the question clearly asks for more.
  - Never answer a demographic question (gender, race, veteran status,
    disability). Return null for those.
  - A required field that combines a yes/no question with a conditional
    request ("Have you ever worked with X? If yes, give your email") is
    answered with the truthful yes/no part alone - "No" - never with the
    detail the conditional asks for.
  - CONDITIONAL QUESTIONS. Many forms ask "If yes, please provide X" right
    after a yes/no question. If the truthful answer to the precondition is no,
    the conditional field MUST be null. Do not put an email, a name or a date
    into a field whose premise is false: filling it asserts the premise. This
    is the single most damaging mistake you can make here, because it states
    something untrue about the candidate's history.
  - If a question asks about prior employment, prior registration, or prior
    contact with this company or its products, and the profile does not record
    it, the answer is no or null. Never assume a prior relationship.
  - The candidate is an EU citizen who works remotely from Sweden and invoices
    through a French micro-entreprise. He needs no visa anywhere in the EU and
    cannot take a role requiring US or UK work authorisation."""


DASHES = re.compile(r"[\u2013\u2014]")


def _scrub(text: str) -> str:
    """Edgar's no-em-dash rule applies to form answers too, not just letters."""
    if not isinstance(text, str):
        return text
    return DASHES.sub(",", text).replace(" ,", ",")


def _client():
    import anthropic
    return anthropic.Anthropic()


def llm_fill(fields: list[Field], unresolved: list[int], job: Job,
             cover_letter: str) -> dict[int, Any]:
    """Ask Claude to answer the fields the rules could not. Never raises."""
    if not unresolved or not config.has_anthropic_key():
        return {}

    p = config.profile()
    payload = {
        "job": {"title": job.title, "company": job.company, "location": job.location,
                "description": job.description[:3000]},
        "cover_letter_already_written": cover_letter[:2000],
        "questions": [
            {"index": i, "question": fields[i].question, "kind": fields[i].kind,
             "required": fields[i].required, "options": fields[i].option_labels()}
            for i in unresolved
        ],
    }
    profile_brief = yaml.safe_dump({
        "identity": {k: v for k, v in p["identity"].items()},
        "preferences": p["preferences"],
        "skills": p["skills"], "experience": p["experience"],
        "projects": [{k: v for k, v in x.items() if k != "note_for_letters"} for x in p["projects"]],
        "education": p["education"],
    }, allow_unicode=True, sort_keys=False)

    schema = {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "answer": {"type": ["string", "null"]},
                        "answers_multi": {"type": ["array", "null"], "items": {"type": "string"}},
                        "confident": {"type": "boolean"},
                    },
                    "required": ["index", "answer", "answers_multi", "confident"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["answers"],
        "additionalProperties": False,
    }

    try:
        response = _client().messages.create(
            model=config.DRAFTING_MODEL,
            max_tokens=8000,
            system=[{"type": "text", "text": SYSTEM + "\n\nCANDIDATE PROFILE:\n" + profile_brief,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user",
                       "content": "Answer these application questions:\n\n"
                                  + json.dumps(payload, ensure_ascii=False)}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            thinking={"type": "adaptive"},
        )
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM form fill failed: %s", exc)
        return {}

    out: dict[int, Any] = {}
    for item in data.get("answers", []):
        idx = item.get("index")
        if idx not in unresolved or not item.get("confident"):
            continue
        if item.get("answers_multi"):
            out[idx] = [_scrub(x) for x in item["answers_multi"]]
        elif item.get("answer") is not None:
            out[idx] = _scrub(item["answer"])
    return out


def plan(fields: list[Field], job: Job, cover_letter: str, cv_path: str
         ) -> tuple[dict[int, Any], list[int]]:
    """Return (answers, unanswered_required_indexes)."""
    answers = deterministic(fields, cover_letter, cv_path)
    unresolved = [i for i, f in enumerate(fields)
                  if i not in answers and f.kind != "file"
                  and not DEMOGRAPHIC.search(f.question or "")]
    answers.update(llm_fill(fields, unresolved, job, cover_letter))

    blocking = [i for i, f in enumerate(fields)
                if f.required and i not in answers and not DEMOGRAPHIC.search(f.question or "")]
    return answers, blocking
