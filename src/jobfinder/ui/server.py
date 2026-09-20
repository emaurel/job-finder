"""Operations view for the job pipeline.

Serves a single page that shows every stage as it happens: which source
returned what, each job being scored with its verdict, each letter being
written, and the approval gate. Actions are posted back over REST; state
arrives over a websocket.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import re

from .. import config, db, delivery, drafting, pipeline, sources
from ..models import Application, Assessment
from .bus import BUS

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Job Finder Ops")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# Only one pipeline stage may run at a time: they share the database and the
# funnel counts would interleave into nonsense.
_run_lock = asyncio.Lock()


@app.on_event("startup")
async def _startup() -> None:
    BUS.bind(asyncio.get_running_loop())
    db.init()


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(str(STATIC / "index.html"))


# --- state ------------------------------------------------------------------

def _funnel() -> dict[str, Any]:
    stats = db.stats()
    with db.connect() as conn:
        reasons = {
            row["r"]: row["n"]
            for row in conn.execute(
                "SELECT substr(filtered_out, 1, instr(filtered_out || ':', ':') - 1) r,"
                " COUNT(*) n FROM jobs WHERE filtered_out != ''"
                " GROUP BY r ORDER BY n DESC LIMIT 10")
        }
        by_source = {
            row["source"]: row["n"]
            for row in conn.execute(
                "SELECT source, COUNT(*) n FROM jobs GROUP BY source ORDER BY n DESC")
        }
        verdicts = {
            row["verdict"]: row["n"]
            for row in conn.execute(
                "SELECT verdict, COUNT(*) n FROM assessments GROUP BY verdict")
        }
    return {"stats": stats, "reasons": reasons, "by_source": by_source,
            "verdicts": verdicts}


SHOT_RE = re.compile(r"Screenshot:\s*(\S+\.png)")


def _queue_rows() -> list[dict[str, Any]]:
    rows = db.queue(statuses=("draft", "approved", "none", "rejected", "sent"),
                    min_score=config.SCORE_SHOW_THRESHOLD)
    out = []
    for row in rows:
        # A job with no application row yet still has a real delivery route:
        # derive it from the posting so an ATS job offers the form button.
        channel = row["channel"]
        recipient = row["recipient"]
        if not channel:
            channel, recipient = drafting.route(db.row_to_job(row))
        out.append({
            "id": row["id"], "title": row["title"], "company": row["company"],
            "location": row["location"], "source": row["source"], "url": row["url"],
            "salary": row["salary"], "score": row["score"], "verdict": row["verdict"],
            "reasoning": row["reasoning"], "strengths": row["strengths"],
            "gaps": row["gaps"], "red_flags": row["red_flags"],
            "status": row["status"] or "none", "channel": channel or "manual",
            "recipient": recipient or "",
            "cover_letter": row["cover_letter"] or "", "notes": row["notes"] or "",
            # The email exactly as delivery.send_application would build it, so
            # the card shows what actually goes out, not just the body.
            "subject": row["subject"] or delivery.fallback_subject(db.row_to_job(row)),
            "signature": (delivery.signature(attached=(channel == "email"))
                          if row["cover_letter"] else ""),
            "attachment": delivery.cv_filename() if delivery.CV_PATH.exists() else "",
            "language": row["posting_language"] or "en", "model": row["model"] or "",
            # Survives a page reload: the filename lives in the saved notes.
            "screenshot": (SHOT_RE.search(row["notes"] or "") or [None, ""])[1]
            if SHOT_RE.search(row["notes"] or "") else "",
        })
    return out


def snapshot() -> dict[str, Any]:
    return {
        "type": "snapshot",
        "funnel": _funnel(),
        "queue": _queue_rows(),
        "log": list(BUS.history),
        "running": BUS.running,
        "progress": BUS.progress,
        "config": {
            "require_approval": config.REQUIRE_APPROVAL,
            "ats_submit": config.ATS_SUBMIT,
            "max_sends_per_day": config.MAX_SENDS_PER_DAY,
            "draft_threshold": config.SCORE_DRAFT_THRESHOLD,
            "show_threshold": config.SCORE_SHOW_THRESHOLD,
            "has_llm": config.has_anthropic_key(),
            "smtp": delivery.email_smtp.configured(),
            "scoring_model": config.SCORING_MODEL,
            "drafting_model": config.DRAFTING_MODEL,
        },
        "sources": [{"name": s.name, "enabled": s.enabled()} for s in sources.ALL_SOURCES],
    }


@app.get("/api/snapshot")
async def api_snapshot() -> JSONResponse:
    return JSONResponse(snapshot())


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = BUS.subscribe()
    try:
        await websocket.send_text(json.dumps(snapshot(), default=str))
        while True:
            event = await queue.get()
            await websocket.send_text(json.dumps(event, default=str))
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        BUS.unsubscribe(queue)


def _push_state() -> None:
    BUS.publish("state", funnel=_funnel(), queue=_queue_rows())


# --- running stages ---------------------------------------------------------

STAGES = {
    "fetch": lambda: pipeline.fetch(),
    "score": lambda: pipeline.score(),
    "draft": lambda: pipeline.draft(),
    "reroute": lambda: pipeline.reroute(),
}


async def _run(name: str, fn) -> dict[str, Any]:
    if _run_lock.locked():
        raise HTTPException(409, f"already running {BUS.running or 'a stage'}")
    async with _run_lock:
        try:
            result = await asyncio.to_thread(fn)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s failed", name)
            BUS.log(f"{name} failed: {exc}", level="error")
            BUS.stage(name, "done", error=str(exc))
            _push_state()
            raise HTTPException(500, str(exc)) from exc
        _push_state()
        return result


@app.post("/api/run/{stage}")
async def run_stage(stage: str) -> JSONResponse:
    if stage == "all":
        async def _all() -> dict[str, Any]:
            out = {}
            for name in ("fetch", "score", "draft"):
                out[name] = await _run(name, STAGES[name])
            return out
        return JSONResponse(await _all())
    if stage not in STAGES:
        raise HTTPException(404, f"unknown stage {stage!r}")
    return JSONResponse(await _run(stage, STAGES[stage]))


# --- per-job actions --------------------------------------------------------

def _row_or_404(job_id: str) -> dict[str, Any]:
    for row in db.queue(statuses=("draft", "approved", "none", "rejected", "sent")):
        if row["id"] == job_id:
            return row
    raise HTTPException(404, "no such job in the queue")


def _application(row: dict[str, Any]) -> Application:
    return Application(
        job_id=row["id"], status=row["status"] or "draft",
        channel=row["channel"] or "manual", subject=row["subject"] or "",
        cover_letter=row["cover_letter"] or "", recipient=row["recipient"] or "")


@app.post("/api/jobs/{job_id}/approve")
async def approve(job_id: str) -> JSONResponse:
    row = _row_or_404(job_id)
    if not row["status"]:
        job = db.row_to_job(row)
        channel, recipient = drafting.route(job)
        db.save_application(Application(job_id=job_id, status="approved",
                                        channel=channel, recipient=recipient,
                                        notes="approved without a drafted letter"))
    else:
        db.set_status(job_id, "approved")
    db.log("approved", job_id=job_id)
    BUS.log(f"approved: {row['title']} at {row['company']}", level="ok")
    _push_state()
    return JSONResponse({"ok": True})


@app.post("/api/jobs/{job_id}/reject")
async def reject(job_id: str) -> JSONResponse:
    row = _row_or_404(job_id)
    if not row["status"]:
        db.save_application(Application(job_id=job_id, status="rejected", channel="manual"))
    else:
        db.set_status(job_id, "rejected")
    db.log("rejected", job_id=job_id)
    BUS.log(f"rejected: {row['title']} at {row['company']}", level="dim")
    _push_state()
    return JSONResponse({"ok": True})


@app.post("/api/jobs/{job_id}/draft")
async def draft_one(job_id: str) -> JSONResponse:
    row = _row_or_404(job_id)
    job = db.row_to_job(row)

    def _work() -> str:
        BUS.stage("draft", "start", total=1)
        BUS.log(f"writing letter: {job.summary_line()}", level="dim")
        assessment = Assessment(
            job_id=job_id, score=row["score"], verdict=row["verdict"],
            reasoning=row["reasoning"] or "", strengths=row["strengths"],
            gaps=row["gaps"], red_flags=row["red_flags"],
            posting_language=row["posting_language"] or "en")
        application = drafting.draft(job, assessment)
        if application is None:
            BUS.log("draft failed", level="error")
            BUS.stage("draft", "done", drafted=0, failed=1)
            return "failed"
        db.save_application(application)
        BUS.log(f"drafted -> {application.channel}", level="ok")
        BUS.stage("draft", "done", drafted=1, failed=0)
        return "drafted"

    result = await _run("draft", _work)
    return JSONResponse({"ok": result == "drafted", "result": result})


@app.post("/api/jobs/{job_id}/ats")
async def ats_fill(job_id: str) -> JSONResponse:
    """Fill the form in a browser and screenshot it. Never submits."""
    row = _row_or_404(job_id)
    job = db.row_to_job(row)
    application = _application(row)
    application.channel = "ats"
    application.recipient = row["recipient"] or job.apply_url

    def _work() -> dict[str, Any]:
        from ..delivery.ats_browser import apply_to_job
        BUS.stage("ats", "start", total=1)
        BUS.log(f"opening browser: {job.company} form", level="dim")
        result = apply_to_job(job, application, submit=False,
                              headless=config.ATS_HEADLESS)
        level = {"prepared": "ok", "blocked": "warn"}.get(result.action, "error")
        BUS.log(f"{result.action}: {result.message}", level=level)
        for item in result.blocking:
            BUS.log(f"  blocked on: {item}", level="warn")
        note = f"{result.action}: {result.message}"
        if result.blocking:
            note += "\nBlocked on: " + "; ".join(result.blocking[:5])
        if result.screenshot:
            note += f"\nScreenshot: {Path(result.screenshot).name}"
        application.notes = note
        application.status = row["status"] or "draft"
        db.save_application(application)
        BUS.stage("ats", "done", action=result.action)
        return {"action": result.action, "message": result.message,
                "blocking": result.blocking, "filled": result.filled,
                "screenshot": Path(result.screenshot).name if result.screenshot else ""}

    return JSONResponse(await _run("ats", _work))


@app.post("/api/jobs/{job_id}/send")
async def send_one(job_id: str) -> JSONResponse:
    row = _row_or_404(job_id)
    if row["status"] != "approved":
        raise HTTPException(400, f"status is {row['status']!r}, not 'approved'")
    job = db.row_to_job(row)
    application = _application(row)

    def _work() -> str:
        BUS.stage("send", "start", total=1)
        try:
            message = delivery.send_application(job, application)
            BUS.log(f"{job.summary_line()}: {message}", level="ok")
        except Exception as exc:  # noqa: BLE001
            db.set_status(job_id, "failed", notes=str(exc))
            BUS.log(f"send failed: {exc}", level="error")
            message = f"failed: {exc}"
        BUS.stage("send", "done")
        return message

    return JSONResponse({"result": await _run("send", _work)})


@app.post("/api/jobs/{job_id}/applied")
async def mark_applied(job_id: str) -> JSONResponse:
    """Record that Edgar submitted this one himself. Sends nothing."""
    row = _row_or_404(job_id)
    db.set_status(job_id, "sent", notes="submitted manually by Edgar")
    db.log("manual_applied", f"{row['title']} at {row['company']}", job_id)
    BUS.log(f"marked as applied: {row['title']} at {row['company']}", level="ok")
    _push_state()
    return JSONResponse({"ok": True})


@app.get("/api/screenshot/{name}")
async def screenshot(name: str) -> FileResponse:
    path = (config.OUTPUT_DIR / "screenshots" / name).resolve()
    shots = (config.OUTPUT_DIR / "screenshots").resolve()
    if not str(path).startswith(str(shots)) or not path.exists():
        raise HTTPException(404, "no such screenshot")
    return FileResponse(str(path))
