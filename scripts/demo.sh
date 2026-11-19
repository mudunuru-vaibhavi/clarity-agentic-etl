#!/usr/bin/env bash
# End-to-end demo: 78 nights of Clarity extracts, two real-world failures, the agent
# handling them, and the reporting marts the dashboard reads. Runs offline in under a minute.
set -euo pipefail
cd "$(dirname "$0")/.."
etl() { uv run clarity-etl "$@"; }

rm -rf data
echo "== 1. Simulate Clarity: write nightly extract drops"
etl generate

echo "== 2. Nightly loads through Nov 10"
etl backfill --until 2026-11-10 | tail -1

echo "== 3. Nov 11: a file is damaged on its way over SFTP"
etl break transfer_corruption --date 2026-11-11
etl backfill --until 2026-11-13 --with-agent | grep -v "^2026" || true

echo "== 4. Nov 14: Epic adds a PAT_EMAIL column without telling anyone"
etl break schema_drift --date 2026-11-14
etl backfill --with-agent || true

echo "== 5. A person reviews the incident: the Clarity team confirms the column was a"
echo "==    mistake and re-sends a clean drop. The pipeline catches up on its own."
etl generate --only-date 2026-11-14 > /dev/null
rm -rf data/landing/2026-11-14
etl backfill | tail -2

echo "== 6. Build the static dashboard"
etl site

echo "== Done. Open site/index.html in a browser (or: uv run streamlit run dashboard/app.py)"
