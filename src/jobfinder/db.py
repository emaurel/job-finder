"""SQLite store. One file, no migrations framework, safe to delete and rebuild."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from .config import DB_PATH, ensure_dirs
from .models import Application, Assessment, Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    external_id   TEXT,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT,
    url           TEXT,
    apply_url     TEXT,
    description   TEXT,
    salary        TEXT,
    posted_at     TEXT,
    is_remote     INTEGER DEFAULT 0,
    apply_email   TEXT,
    ats           TEXT,
    raw           TEXT,
    first_seen_at TEXT NOT NULL,
    filtered_out  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS assessments (
    job_id           TEXT PRIMARY KEY REFERENCES jobs(id),
    score            INTEGER NOT NULL,
    verdict          TEXT NOT NULL,
    reasoning        TEXT,
    strengths        TEXT,
    gaps             TEXT,
    red_flags        TEXT,
    posting_language TEXT,
    model            TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    job_id       TEXT PRIMARY KEY REFERENCES jobs(id),
    status       TEXT NOT NULL,
    channel      TEXT NOT NULL,
    subject      TEXT,
    cover_letter TEXT,
    recipient    TEXT,
    notes        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    sent_at      TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT,
    kind       TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_seen  ON jobs(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, created_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def upsert_job(conn: sqlite3.Connection, job: Job, filtered_out: str = "") -> bool:
    """Insert a job. Returns True if it was new."""
    existing = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job.id,)).fetchone()
    if existing:
        return False
    conn.execute(
        """INSERT INTO jobs (id, source, external_id, title, company, location, url,
               apply_url, description, salary, posted_at, is_remote, apply_email, ats,
               raw, first_seen_at, filtered_out)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job.id, job.source, job.external_id, job.title, job.company, job.location,
         job.url, job.apply_url, job.description, job.salary, job.posted_at,
         int(job.is_remote), job.apply_email, job.ats, json.dumps(job.raw)[:200000],
         now(), filtered_out),
    )
    return True


def row_to_job(row: sqlite3.Row) -> Job:
    job = Job(
        source=row["source"], external_id=row["external_id"] or "", title=row["title"],
        company=row["company"], location=row["location"] or "", url=row["url"] or "",
        description=row["description"] or "", apply_url=row["apply_url"] or "",
        salary=row["salary"] or "", posted_at=row["posted_at"] or "",
        is_remote=bool(row["is_remote"]), apply_email=row["apply_email"] or "",
        ats=row["ats"] or "",
    )
    return job


def unscored_jobs(limit: int = 100) -> list[Job]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT j.* FROM jobs j
               LEFT JOIN assessments a ON a.job_id = j.id
               WHERE a.job_id IS NULL AND j.filtered_out = ''
               ORDER BY j.first_seen_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [row_to_job(r) for r in rows]


def save_assessment(a: Assessment) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO assessments
               (job_id, score, verdict, reasoning, strengths, gaps, red_flags,
                posting_language, model, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (a.job_id, a.score, a.verdict, a.reasoning, json.dumps(a.strengths),
             json.dumps(a.gaps), json.dumps(a.red_flags), a.posting_language,
             a.model, a.created_at),
        )


def save_application(app: Application) -> None:
    with connect() as conn:
        existing = conn.execute(
            "SELECT created_at FROM applications WHERE job_id = ?", (app.job_id,)
        ).fetchone()
        created = existing["created_at"] if existing else now()
        conn.execute(
            """INSERT OR REPLACE INTO applications
               (job_id, status, channel, subject, cover_letter, recipient, notes,
                created_at, updated_at, sent_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (app.job_id, app.status, app.channel, app.subject, app.cover_letter,
             app.recipient, app.notes, created, now(), app.sent_at),
        )


def set_status(job_id: str, status: str, notes: str = "") -> None:
    with connect() as conn:
        sent = now() if status == "sent" else None
        conn.execute(
            "UPDATE applications SET status=?, updated_at=?, sent_at=COALESCE(?, sent_at)"
            + (", notes=?" if notes else "")
            + " WHERE job_id=?",
            ((status, now(), sent, notes, job_id) if notes else (status, now(), sent, job_id)),
        )


def log(kind: str, detail: str = "", job_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO events (job_id, kind, detail, created_at) VALUES (?,?,?,?)",
            (job_id, kind, detail, now()),
        )


def sent_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM applications WHERE status='sent' AND sent_at LIKE ?",
            (f"{today}%",),
        ).fetchone()
    return row["c"]


def queue(statuses: tuple[str, ...] = ("draft",), min_score: int = 0) -> list[dict[str, Any]]:
    """Jobs with an assessment, joined with any application, newest-scored first."""
    placeholders = ",".join("?" for _ in statuses)
    sql = f"""
        SELECT j.*, a.score, a.verdict, a.reasoning, a.strengths, a.gaps,
               a.red_flags, a.posting_language, a.model,
               p.status, p.channel, p.subject, p.cover_letter, p.recipient,
               p.notes, p.sent_at
        FROM jobs j
        JOIN assessments a ON a.job_id = j.id
        LEFT JOIN applications p ON p.job_id = j.id
        WHERE a.score >= ?
          AND (p.status IN ({placeholders}) OR (p.status IS NULL AND 'none' IN ({placeholders})))
        ORDER BY a.score DESC, j.first_seen_at DESC
    """
    with connect() as conn:
        rows = conn.execute(sql, (min_score, *statuses, *statuses)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for key in ("strengths", "gaps", "red_flags"):
            try:
                d[key] = json.loads(d[key] or "[]")
            except (TypeError, json.JSONDecodeError):
                d[key] = []
        out.append(d)
    return out


def stats() -> dict[str, Any]:
    with connect() as conn:
        def one(sql: str, args: tuple = ()) -> int:
            return conn.execute(sql, args).fetchone()[0]
        return {
            "jobs": one("SELECT COUNT(*) FROM jobs"),
            "filtered": one("SELECT COUNT(*) FROM jobs WHERE filtered_out != ''"),
            "scored": one("SELECT COUNT(*) FROM assessments"),
            "drafted": one("SELECT COUNT(*) FROM applications WHERE status='draft'"),
            "approved": one("SELECT COUNT(*) FROM applications WHERE status='approved'"),
            "sent": one("SELECT COUNT(*) FROM applications WHERE status='sent'"),
            "rejected": one("SELECT COUNT(*) FROM applications WHERE status='rejected'"),
            "sent_today": sent_today(),
        }
