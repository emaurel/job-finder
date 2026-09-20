"""The stages: fetch -> filter -> score -> draft -> (approve) -> send."""
from __future__ import annotations

import logging

from . import config, db, delivery, drafting, filters, scoring, sources
from .ui.bus import BUS
from .models import Application, Assessment, Job

log = logging.getLogger(__name__)


def dedupe(jobs: list[Job]) -> list[Job]:
    """Collapse the same role listed once per country into one entry.

    GitLab and Elastic post an identical opening for every country they hire in.
    Keep the variant with the most EU-friendly location so the link Edgar opens
    is one he can actually apply through.
    """
    best: dict[tuple[str, str], Job] = {}
    for job in jobs:
        key = (job.company.lower(), job.title.lower())
        current = best.get(key)
        if current is None:
            best[key] = job
            continue
        if filters.REMOTE_OK_REGION.search(job.location) and not \
                filters.REMOTE_OK_REGION.search(current.location):
            best[key] = job
    return list(best.values())


def fetch(only: list[str] | None = None) -> dict[str, int]:
    """Pull every configured source, dedupe, apply hard filters, store."""
    db.init()
    BUS.stage("fetch", "start")

    found: list[Job] = []
    for source in sources.ALL_SOURCES:
        if only and source.name not in only:
            continue
        if not source.enabled():
            BUS.log(f"{source.name}: not configured, skipped", level="dim")
            continue
        try:
            got = list(source.fetch())
        except Exception as exc:  # noqa: BLE001
            BUS.log(f"{source.name}: failed ({exc})", level="error")
            continue
        BUS.log(f"{source.name}: {len(got)} postings", level="ok", source=source.name,
                count=len(got))
        found.extend(got)

    jobs = dedupe(found)
    BUS.log(f"deduped {len(found)} -> {len(jobs)}", level="dim")

    new = kept = rejected = 0
    reasons: dict[str, int] = {}
    with db.connect() as conn:
        for i, job in enumerate(jobs):
            reason = filters.reject_reason(job)
            if reason:
                reasons[reason.split(":")[0]] = reasons.get(reason.split(":")[0], 0) + 1
            if db.upsert_job(conn, job, filtered_out=reason):
                new += 1
                if reason:
                    rejected += 1
                else:
                    kept += 1
                    BUS.publish("job", id=job.id, title=job.title, company=job.company,
                                stage="kept")
            if i % 200 == 0:
                BUS.tick(i, len(jobs), "filtering")

    result = {"seen": len(jobs), "new": new, "kept": kept, "filtered": rejected,
              "reasons": reasons}
    db.log("fetch", f"seen={len(jobs)} new={new} kept={kept} filtered={rejected}")
    BUS.stage("fetch", "done", **result)
    return result


def score(limit: int = 100) -> dict[str, int]:
    """Score everything that survived the filters and has not been scored."""
    db.init()
    pending = db.unscored_jobs(limit)
    BUS.stage("score", "start", total=len(pending))
    counts = {"scored": 0, "apply": 0, "maybe": 0, "skip": 0}
    for i, job in enumerate(pending, 1):
        assessment = scoring.score_job(job)
        db.save_assessment(assessment)
        counts["scored"] += 1
        counts[assessment.verdict] = counts.get(assessment.verdict, 0) + 1
        level = {"apply": "ok", "maybe": "warn"}.get(assessment.verdict, "dim")
        BUS.log(f"{assessment.score:3d} {assessment.verdict:5} {job.summary_line()}",
                level=level, job_id=job.id, score=assessment.score)
        BUS.publish("job", id=job.id, stage="scored", score=assessment.score,
                    verdict=assessment.verdict, title=job.title, company=job.company)
        BUS.tick(i, len(pending), job.company)
        log.info("%3d %-5s %s", assessment.score, assessment.verdict, job.summary_line())
    db.log("score", str(counts))
    BUS.stage("score", "done", **counts)
    return counts


def draft(limit: int = 20, min_score: int | None = None) -> dict[str, int]:
    """Write letters for high scorers that do not have one yet."""
    db.init()
    threshold = config.SCORE_DRAFT_THRESHOLD if min_score is None else min_score
    candidates = db.queue(statuses=("none",), min_score=threshold)[:limit]
    BUS.stage("draft", "start", total=len(candidates))
    if not config.has_anthropic_key():
        log.warning("no ANTHROPIC_API_KEY: %d jobs are waiting for a letter", len(candidates))
        BUS.log("no ANTHROPIC_API_KEY, cannot write letters", level="error")
        BUS.stage("draft", "done", drafted=0, skipped_no_api_key=len(candidates))
        return {"drafted": 0, "skipped_no_api_key": len(candidates)}
    counts = {"drafted": 0, "failed": 0}
    for row in candidates:
        job = db.row_to_job(row)
        assessment = Assessment(
            job_id=job.id, score=row["score"], verdict=row["verdict"],
            reasoning=row["reasoning"] or "", strengths=row["strengths"],
            gaps=row["gaps"], red_flags=row["red_flags"],
            posting_language=row["posting_language"] or "en",
        )
        BUS.log(f"writing letter: {job.summary_line()}", level="dim")
        app = drafting.draft(job, assessment)
        if app is None:
            counts["failed"] += 1
            BUS.log(f"draft failed: {job.summary_line()}", level="error")
            continue
        db.save_application(app)
        counts["drafted"] += 1
        BUS.log(f"drafted -> {app.channel}: {app.recipient[:60]}", level="ok",
                job_id=job.id)
        BUS.publish("job", id=job.id, stage="drafted", channel=app.channel)
        BUS.tick(counts["drafted"] + counts["failed"], len(candidates), job.company)
        log.info("drafted %s -> %s via %s", job.summary_line(), app.recipient, app.channel)
    db.log("draft", str(counts))
    BUS.stage("draft", "done", **counts)
    return counts


def reroute() -> dict[str, int]:
    """Re-check delivery routes for applications still marked manual.

    Aggregator listings often turn out to have a real ATS form behind them; this
    upgrades the ones that do, so the browser agent can fill them.
    """
    db.init()
    rows = [r for r in db.queue(statuses=("none", "draft", "approved"))
            if (r["channel"] or "manual") == "manual"]
    BUS.stage("reroute", "start", total=len(rows))
    counts = {"checked": len(rows), "upgraded": 0}
    for i, row in enumerate(rows, 1):
        job = db.row_to_job(row)
        channel, recipient = drafting.route(job)
        if channel == "ats":
            app = Application(
                job_id=job.id, status=row["status"] or "draft", channel="ats",
                recipient=recipient, subject=row["subject"] or "",
                cover_letter=row["cover_letter"] or "", notes=row["notes"] or "")
            db.save_application(app)
            counts["upgraded"] += 1
            BUS.log(f"upgraded to a real form: {job.summary_line()}", level="ok")
        BUS.tick(i, len(rows), job.company)
    BUS.stage("reroute", "done", **counts)
    return counts


def send(dry_run: bool = False, limit: int | None = None) -> list[str]:
    """Send everything marked approved, up to the daily cap."""
    db.init()
    approved = db.queue(statuses=("approved",))
    results: list[str] = []
    for row in approved[: limit or len(approved)]:
        job = db.row_to_job(row)
        app = Application(
            job_id=job.id, status=row["status"], channel=row["channel"] or "manual",
            subject=row["subject"] or "", cover_letter=row["cover_letter"] or "",
            recipient=row["recipient"] or "",
        )
        try:
            results.append(f"{job.summary_line()}: {delivery.send_application(job, app, dry_run=dry_run)}")
        except delivery.DailyCapReached as exc:
            results.append(f"STOPPED: {exc}")
            break
        except Exception as exc:  # noqa: BLE001
            db.set_status(job.id, "failed", notes=str(exc))
            results.append(f"{job.summary_line()}: FAILED {exc}")
    return results


def run_all(dry_run: bool = True) -> dict[str, object]:
    """One full cycle. Sending still requires approvals to already exist."""
    out: dict[str, object] = {}
    out["fetch"] = fetch()
    out["score"] = score()
    out["draft"] = draft()
    out["send"] = send(dry_run=dry_run)
    out["stats"] = db.stats()
    return out
