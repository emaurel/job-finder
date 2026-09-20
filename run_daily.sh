#!/usr/bin/env bash
# Daily cycle: find, screen, draft. Never sends - approval happens in the UI.
# Install:  crontab -e
#   0 8 * * 1-5  /home/edgar/sweden/job_finder/run_daily.sh >> /home/edgar/sweden/job_finder/data/cron.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH=src
echo "=== $(date -Is) ==="
.venv/bin/python -m jobfinder.cli fetch
.venv/bin/python -m jobfinder.cli score
.venv/bin/python -m jobfinder.cli draft
.venv/bin/python -m jobfinder.cli status
