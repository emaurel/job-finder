"""Command line for the job finder."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import config, db, pipeline


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(message)s",
        stream=sys.stderr,
    )


def cmd_fetch(args) -> None:
    only = args.source.split(",") if args.source else None
    print(json.dumps(pipeline.fetch(only), indent=2))


def cmd_score(args) -> None:
    print(json.dumps(pipeline.score(limit=args.limit), indent=2))


def cmd_draft(args) -> None:
    print(json.dumps(pipeline.draft(limit=args.limit, min_score=args.min_score), indent=2))


def cmd_reroute(args) -> None:
    print(json.dumps(pipeline.reroute(), indent=2))


def cmd_send(args) -> None:
    for line in pipeline.send(dry_run=args.dry_run, limit=args.limit):
        print(line)


def cmd_run(args) -> None:
    print(json.dumps(pipeline.run_all(dry_run=not args.live), indent=2, default=str))


def cmd_status(args) -> None:
    stats = db.stats()
    print(f"""Job finder status
  jobs seen          {stats['jobs']}
  filtered out       {stats['filtered']}
  scored             {stats['scored']}
  drafts waiting     {stats['drafted']}
  approved to send   {stats['approved']}
  sent (total)       {stats['sent']}
  sent today         {stats['sent_today']} / {config.MAX_SENDS_PER_DAY}
  rejected by you    {stats['rejected']}

  LLM key present    {config.has_anthropic_key()}
  approval required  {config.REQUIRE_APPROVAL}
  ATS auto-submit    {config.ATS_SUBMIT}""")


def cmd_list(args) -> None:
    rows = db.queue(statuses=tuple(args.status.split(",")), min_score=args.min_score)
    if not rows:
        print("nothing matching")
        return
    for row in rows[: args.limit]:
        flags = " ".join(f"[{f}]" for f in row["red_flags"][:2])
        print(f"{row['score']:3d} {row['verdict']:5} {row['status'] or 'none':9} "
              f"{row['title'][:48]:48} {row['company'][:22]:22} {flags}")
        print(f"     {row['url']}")


def cmd_ats(args) -> None:
    from .delivery.ats_browser import apply_to_job
    from .models import Application

    rows = db.queue(statuses=("draft", "approved", "none"))
    row = next((r for r in rows if r["id"].startswith(args.job)), None)
    if row is None:
        print(f"no job in the queue starting with {args.job!r}")
        return
    job = db.row_to_job(row)
    app = Application(
        job_id=job.id, status=row["status"] or "draft", channel="ats",
        recipient=row["recipient"] or job.apply_url,
        subject=row["subject"] or "", cover_letter=row["cover_letter"] or "")

    if args.submit and app.status != "approved":
        print(f"refusing to submit: status is {app.status!r}, not 'approved'")
        return

    print(f"{job.summary_line()}\n{app.recipient}\n")
    result = apply_to_job(job, app, submit=args.submit, headless=not args.show)
    print(f"  action     {result.action}")
    print(f"  message    {result.message}")
    print(f"  filled     {result.filled}")
    if result.blocking:
        print("  BLOCKED ON:")
        for item in result.blocking:
            print(f"    - {item}")
    if result.unfilled_optional:
        print(f"  left empty (optional): {len(result.unfilled_optional)}")
    print(f"  screenshot {result.screenshot}")


def cmd_add_company(args) -> None:
    from .add_company import add, resolve

    hits = resolve(args.target)
    if not hits:
        print(f"No live Greenhouse, Lever or Ashby board found for {args.target!r}.")
        print("Open one of their job postings and paste the URL instead, e.g.")
        print("  jf add-company https://jobs.ashbyhq.com/legora/1234")
        print("If they use Workday, Teamtailor or a custom site, we cannot read it.")
        return
    if len(hits) > 1:
        hits.sort(key=lambda h: -h[2])
        print(f"{args.target!r} matched several platforms, taking the biggest:")
        for ats, slug, n in hits:
            print(f"   {ats:11} {slug:22} {n} openings")
    ats, slug, n = hits[0]
    print(add(ats, slug) + f"  ({n} openings live right now)")
    print("Run `jf fetch` to pull them in.")


def cmd_ui(args) -> None:
    import uvicorn
    from .ui.server import app
    print(f"Job Finder ops view:  http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobfinder", description="Find and apply to jobs.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="pull postings from every configured source")
    p.add_argument("--source", help="comma separated subset, e.g. jobtech,ashby")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("score", help="screen unscored jobs against the profile")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("draft", help="write cover letters for high scorers")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--min-score", type=int, default=None)
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("reroute",
                       help="re-check manual listings for a real ATS form behind them")
    p.set_defaults(func=cmd_reroute)

    p = sub.add_parser("send", help="send approved applications")
    p.add_argument("--dry-run", action="store_true", help="print instead of sending")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("run", help="fetch + score + draft, then send (dry by default)")
    p.add_argument("--live", action="store_true", help="actually send approved applications")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("list", help="show the queue")
    p.add_argument("--status", default="none,draft,approved")
    p.add_argument("--min-score", type=int, default=0)
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("ats", help="fill a Greenhouse/Lever/Ashby form in a browser")
    p.add_argument("job", help="job id, or a unique prefix of one")
    p.add_argument("--submit", action="store_true",
                   help="actually click Submit (requires an approved application)")
    p.add_argument("--show", action="store_true", help="run the browser visibly")
    p.set_defaults(func=cmd_ats)

    p = sub.add_parser("status", help="counts and configuration")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("add-company",
                       help="add a company's job board by name or careers URL")
    p.add_argument("target", help="company name, or any URL from their job board")
    p.set_defaults(func=cmd_add_company)

    p = sub.add_parser("ui", aliases=["review"],
                       help="open the ops view: run stages, watch them, approve")
    p.add_argument("--port", type=int, default=8420)
    p.set_defaults(func=cmd_ui)

    args = parser.parse_args(argv)
    _log(args.verbose)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
