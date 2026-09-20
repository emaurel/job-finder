"""Send an application by email over SMTP.

Gmail: create an app password at https://myaccount.google.com/apppasswords and
put it in SMTP_PASSWORD. Normal account passwords will not work.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)


def configured() -> bool:
    return bool(config.env("SMTP_USER") and config.env("SMTP_PASSWORD"))


def send(to: str, subject: str, body: str, attachments: list[Path] | None = None,
         attachment_name: str | None = None) -> None:
    """Send, or raise. The caller owns the approval gate and the daily cap."""
    if not configured():
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD not set in .env")

    msg = EmailMessage()
    from_name = config.env("FROM_NAME", "Edgar Maurel")
    sender = config.env("SMTP_USER")
    msg["From"] = f"{from_name} <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    reply_to = config.env("REPLY_TO")
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)

    for path in attachments or []:
        if not path.exists():
            log.warning("attachment missing, skipping: %s", path)
            continue
        msg.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="pdf" if path.suffix.lower() == ".pdf" else "octet-stream",
            filename=attachment_name or path.name,
        )

    host = config.env("SMTP_HOST", "smtp.gmail.com")
    port = config.env_int("SMTP_PORT", 587)
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(sender, config.env("SMTP_PASSWORD"))
        server.send_message(msg)
    log.info("sent to %s: %s", to, subject)
