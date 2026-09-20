"""Drive a Greenhouse / Lever / Ashby application form with Playwright.

Design rule, borrowed from how the rest of this project handles sending:
the submit click is the only irreversible act here, so it is gated three ways
and always preceded by a screenshot. A run that produced no screenshot is a
failed run, whatever the return value says.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config, db
from ..models import Application, Job
from . import ats_forms
from .ats_forms import EXTRACT_JS, Field

log = logging.getLogger(__name__)

SHOTS = config.OUTPUT_DIR / "screenshots"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

SUBMIT_TEXTS = [
    "submit application", "submit your application", "submit",
    "send application", "apply now", "apply for this job", "apply",
]


@dataclass
class AtsResult:
    ok: bool
    action: str                     # submitted | prepared | blocked | error
    message: str
    screenshot: str = ""
    filled: int = 0
    blocking: list[str] = field(default_factory=list)
    unfilled_optional: list[str] = field(default_factory=list)


def _shot(page, job: Job, tag: str) -> str:
    SHOTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-z0-9]+", "-", f"{job.company}-{job.title}".lower())[:60]
    path = SHOTS / f"{stamp}-{safe}-{tag}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("screenshot failed: %s", exc)
        return ""
    return str(path)


def _sel(jf: int) -> str:
    return f'[data-jf="{jf}"]'


def _fill_one(page, f: Field, value: Any) -> bool:
    """Put one answer into the page. Returns True if it landed."""
    try:
        if f.kind == "file":
            path = value["file"] if isinstance(value, dict) else value
            if not Path(path).exists():
                return False
            page.set_input_files(_sel(f.jf), path, timeout=15000)
            return True

        if f.kind in {"text", "textarea"}:
            # Location fields are autocompletes even when they advertise no ARIA
            # combobox role (Lever does exactly this), and a plain fill is
            # silently discarded on submit. Try the picker, then fall back.
            if f.combobox or PICKER_Q.search(f.question or ""):
                if _fill_combobox(page, f, str(value)):
                    return True
            page.fill(_sel(f.jf), str(value), timeout=15000)
            return True

        if f.kind == "select":
            try:
                page.select_option(_sel(f.jf), label=str(value), timeout=10000)
            except Exception:
                page.select_option(_sel(f.jf), value=str(value), timeout=10000)
            return True

        if f.kind in {"radio", "checkbox"}:
            wanted = value if isinstance(value, list) else [value]
            landed = False
            for option in f.options:
                if option["label"] not in wanted:
                    continue
                if _check(page, option["jf"]):
                    landed = True
            return landed
    except Exception as exc:  # noqa: BLE001
        log.debug("could not fill %r: %s", f.question[:40], exc)
    return False


def _check(page, jf: int) -> bool:
    """Tick a radio or checkbox, even when the real input is visually hidden."""
    locator = page.locator(_sel(jf))
    try:
        locator.check(force=True, timeout=8000)
        return True
    except Exception:
        pass
    try:
        # Styled forms hide the input and put the click target on its label.
        element_id = locator.get_attribute("id")
        if element_id:
            page.click(f'label[for="{element_id}"]', timeout=8000)
            return True
    except Exception:
        pass
    try:
        locator.dispatch_event("click")
        return True
    except Exception:
        return False


def harvest_options(page, fields: list[Field]) -> None:
    """Open every combobox and record what it actually offers.

    Without this the model is guessing at option strings it has never seen, and
    a guess that does not match an option silently fills nothing.
    """
    for f in fields:
        if not f.combobox or f.jf is None or f.select_options:
            continue
        try:
            page.click(_sel(f.jf), timeout=6000)
            page.wait_for_timeout(700)
            options = _options_locator(page)
            labels = []
            for i in range(0 if options is None else min(options.count(), 60)):
                text = (options.nth(i).inner_text() or "").strip()
                if text:
                    labels.append(text)
            f.select_options = labels
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not harvest options for %r: %s", f.question[:40], exc)


# Questions whose field is usually an autocomplete rather than a plain input.
PICKER_Q = re.compile(r"\b(location|city|country|based|town)\b", re.I)


def _fold(text: str) -> str:
    norm = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(c for c in norm if not unicodedata.combining(c))


# Every ATS renders its autocomplete results differently, and a field that
# clears itself on blur (Lever) makes picking one mandatory, not optional.
OPTION_SELECTORS = [
    '[role="option"]:visible',
    '.dropdown-results div:visible',
    '[class*="dropdown"] [class*="result"]:visible',
    '[role="listbox"] li:visible',
    'ul[class*="autocomplete"] li:visible',
]


def _options_locator(page):
    for selector in OPTION_SELECTORS:
        try:
            locator = page.locator(selector)
            if locator.count() > 0:
                return locator
        except Exception:
            continue
    return None


def _pick_option(page, wanted: str) -> bool:
    """Click the listbox option matching `wanted`.

    Accent-insensitive, because the profile says "Jonkoping" and the option says
    "Jonkoping". Shortest match wins, or "Jonkoping, Sweden" loses to the suburb
    "Jonkopings Kristina-Ljungarum, Jonkoping, Sweden" that merely contains it.
    """
    options = _options_locator(page)
    if options is None:
        return False
    count = options.count()
    target = _fold(wanted)
    texts = [(i, (options.nth(i).inner_text() or "").strip())
             for i in range(min(count, 60))]

    for i, text in texts:
        if _fold(text) == target:
            options.nth(i).click(timeout=4000)
            return True

    partial = [(i, text) for i, text in texts
               if target in _fold(text) or _fold(text) in target]
    if partial:
        i, _ = min(partial, key=lambda pair: len(pair[1]))
        options.nth(i).click(timeout=4000)
        return True
    return False


def _fill_combobox(page, f: Field, value: str) -> bool:
    """Click the control open and pick an option. Typing alone does not commit,
    and geo autocompletes ignore a programmatic fill() entirely - they only
    query their backend on real keystrokes."""
    locator = page.locator(_sel(f.jf))
    try:
        locator.click(timeout=8000)
        page.wait_for_timeout(600)
        if _pick_option(page, value):
            return True

        # Nothing offered up front: this is a search-as-you-type field.
        for query in _query_variants(value):
            try:
                locator.fill("", timeout=4000)
            except Exception:
                pass
            locator.press_sequentially(query, delay=130)
            page.wait_for_timeout(2400)
            if _pick_option(page, value) or _pick_option(page, query):
                return True

        page.keyboard.press("Escape")
        return False
    except Exception as exc:  # noqa: BLE001
        log.debug("combobox fill failed %r: %s", (f.question or "")[:40], exc)
        return False


def _query_variants(value: str) -> list[str]:
    """Progressively simpler search strings. "Jonkoping, Sweden" finds nothing;
    "Jonkoping" finds the city."""
    value = value.strip()
    variants = [value]
    head = value.split(",")[0].strip()
    if head and head != value:
        variants.append(head)
    folded = unicodedata.normalize("NFKD", head)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    if folded and folded != head:
        variants.append(folded)
    return variants


def _find_submit(page):
    for text in SUBMIT_TEXTS:
        for selector in (f'button:has-text("{text}")', f'input[type=submit][value*="{text}" i]'):
            try:
                locator = page.locator(selector).last
                if locator.count() > 0 and locator.is_visible():
                    return locator
            except Exception:
                continue
    return None


def apply_to_job(job: Job, application: Application, *, submit: bool = False,
                 headless: bool = True, timeout_ms: int = 60000) -> AtsResult:
    """Fill this job's ATS form. Submits ONLY when submit=True and the
    application is already approved."""
    from playwright.sync_api import sync_playwright

    if submit:
        if config.REQUIRE_APPROVAL and application.status != "approved":
            return AtsResult(False, "blocked",
                             f"status is '{application.status}', not 'approved'")
        if db.sent_today() >= config.MAX_SENDS_PER_DAY:
            return AtsResult(False, "blocked",
                             f"daily cap of {config.MAX_SENDS_PER_DAY} reached")

    url = application.recipient or job.apply_url or job.url
    cv_path = str(config.ROOT / "profile" / "cv_source.pdf")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 1600},
                                      locale="en-GB")
        page = context.new_page()
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)

            raw = page.evaluate(EXTRACT_JS)
            if not raw:
                shot = _shot(page, job, "no-form")
                return AtsResult(False, "error", "no form controls found on page",
                                 screenshot=shot)

            fields = ats_forms.build_schema(raw)
            harvest_options(page, fields)
            answers, blocking = ats_forms.plan(fields, job, application.cover_letter, cv_path)

            if blocking:
                labels = [fields[i].question[:70] or f"<field {i}>" for i in blocking]
                shot = _shot(page, job, "blocked")
                return AtsResult(
                    False, "blocked",
                    f"{len(blocking)} required question(s) could not be answered truthfully",
                    screenshot=shot, blocking=labels)

            # The planning step makes an LLM call that can take half a minute.
            # React forms re-render in that window and drop the data-jf tags, so
            # re-tag now and re-key the answers by field identity, not by index.
            by_key = {fields[i].key(): value for i, value in answers.items()}
            fields = ats_forms.build_schema(page.evaluate(EXTRACT_JS))

            filled = 0
            failed: list[str] = []
            for f in fields:
                if f.key() not in by_key:
                    continue
                if _fill_one(page, f, by_key[f.key()]):
                    filled += 1
                else:
                    failed.append(f.question[:70] or f.name)

            # Blur the last control so combobox values commit, then let the
            # form settle before reading anything back.
            try:
                page.keyboard.press("Escape")
                page.locator("body").click(position={"x": 5, "y": 5}, timeout=3000)
            except Exception:
                pass
            page.wait_for_timeout(2000)

            # Verify against the DOM. A fill call returning True proves nothing:
            # controlled inputs silently reject programmatic values.
            after = ats_forms.build_schema(page.evaluate(EXTRACT_JS))
            empty_required, not_landed = [], []
            for f in after:
                intended = by_key.get(f.key())
                if f.kind == "file":
                    continue
                if f.required and not f.value:
                    empty_required.append(f.question[:70] or f.name)
                elif intended is not None and not f.value:
                    not_landed.append(f.question[:70] or f.name)

            shot = _shot(page, job, "submitted" if submit else "filled")

            if empty_required:
                return AtsResult(
                    False, "blocked",
                    f"{len(empty_required)} required field(s) still empty after filling",
                    screenshot=shot, filled=filled, blocking=empty_required,
                    unfilled_optional=not_landed)

            if not submit:
                return AtsResult(True, "prepared",
                                 f"filled {filled} fields and verified, not submitted",
                                 screenshot=shot, filled=filled,
                                 unfilled_optional=failed + not_landed)

            button = _find_submit(page)
            if button is None:
                return AtsResult(False, "error", "could not find a submit button",
                                 screenshot=shot, filled=filled)
            button.click(timeout=20000)
            page.wait_for_timeout(6000)
            confirm_shot = _shot(page, job, "after-submit")
            db.set_status(job.id, "sent")
            db.log("ats_submitted", f"{job.summary_line()} via {job.ats}", job.id)
            return AtsResult(True, "submitted", f"submitted via {job.ats}",
                             screenshot=confirm_shot, filled=filled)

        except Exception as exc:  # noqa: BLE001
            shot = _shot(page, job, "error")
            return AtsResult(False, "error", f"{type(exc).__name__}: {exc}"[:300],
                             screenshot=shot)
        finally:
            context.close()
            browser.close()
