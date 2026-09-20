"""Delivery. Every path through here is gated: nothing sends without an explicit
approved status, and nothing sends past the daily cap."""
from __future__ import annotations

import logging
from pathlib import Path

from .. import config, db
from ..models import Application, Job
from . import email_smtp

log = logging.getLogger(__name__)

CV_PATH = config.ROOT / "profile" / "cv_source.pdf"


def cv_filename() -> str:
    """What the attachment is called in the recipient's inbox."""
    return f"{config.profile()['identity']['full_name']} - CV.pdf"


class NotApproved(RuntimeError):
    pass


class DailyCapReached(RuntimeError):
    pass


def signature(attached: bool = True) -> str:
    """Contact block. `attached` adds the CV line, which is only true for email;
    a web form takes the CV as a separate upload."""
    p = config.profile()["identity"]
    links = p.get("links", {})
    lines = [
        "",
        "-- ",
        p["full_name"],
        p.get("headline", ""),
        "",
        f"Email:     {p['email']}",
        f"Phone:     {p.get('phone', '')}",
    ]
    if links.get("portfolio"):
        lines.append(f"Portfolio: {links['portfolio']}")
    if links.get("github"):
        lines.append(f"GitHub:    {links['github']}")
    if links.get("linkedin"):
        lines.append(f"LinkedIn:  {links['linkedin']}")
    if attached:
        lines.append("")
        lines.append("CV attached as PDF.")
    return "\n".join(lines)


def fallback_subject(job: Job) -> str:
    """Used when an application was approved without a drafted letter, so a
    send can never go out with an empty subject line."""
    name = config.profile()["identity"]["full_name"]
    return f"Application: {job.title} - {name}"


def send_application(job: Job, app: Application, *, dry_run: bool = False) -> str:
    """Send one approved application. Returns a human-readable outcome."""
    if config.REQUIRE_APPROVAL and app.status != "approved":
        raise NotApproved(f"{job.summary_line()} is '{app.status}', not 'approved'")

    if db.sent_today() >= config.MAX_SENDS_PER_DAY:
        raise DailyCapReached(
            f"daily cap of {config.MAX_SENDS_PER_DAY} already reached"
        )

    if app.channel == "ats":
        from .ats_browser import apply_to_job
        submit = bool(config.ATS_SUBMIT and not dry_run)
        result = apply_to_job(job, app, submit=submit,
                              headless=config.ATS_HEADLESS)
        note = f"{result.action}: {result.message}"
        if result.blocking:
            note += " | blocked on: " + "; ".join(result.blocking[:4])
        db.set_status(job.id, "sent" if result.action == "submitted" else "approved",
                      notes=note)
        db.log(f"ats_{result.action}", f"{job.summary_line()} | {result.screenshot}", job.id)
        return f"{note} | screenshot: {result.screenshot}"

    if app.channel != "email":
        # Manual applications are not sent from here. They stay in the queue
        # with everything prepared, and the human submits the form.
        db.set_status(job.id, "approved", notes="awaiting manual submission")
        return f"prepared for manual submission: {app.recipient}"

    body = app.cover_letter + signature()
    if dry_run:
        subject = app.subject or fallback_subject(job)
        return (f"DRY RUN would email {app.recipient}\n"
                f"Subject: {subject}\n"
                f"Attachment: {cv_filename()}\n\n{body}")

    attachments = [CV_PATH] if CV_PATH.exists() else []
    subject = app.subject or fallback_subject(job)
    email_smtp.send(app.recipient, subject, body, attachments,
                    attachment_name=cv_filename())
    db.set_status(job.id, "sent")
    db.log("sent", f"{job.summary_line()} -> {app.recipient}", job.id)
    return f"sent to {app.recipient}"
