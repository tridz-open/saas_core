#!/usr/bin/env bash
# Run saas_core bench tests on a disposable site.
set -euo pipefail

usage() {
  cat <<U
Usage: ADMIN_PASSWORD=... BENCH_DIR=/path/to/frappe-bench scripts/bench_test.sh

Env:
  BENCH_DIR       bench directory (required)
  ADMIN_PASSWORD  admin password for new site (required)
  SITE            site name (default: saas-test.localhost)
  APP_PATH        saas_core source path (default: repo root)
U
}
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }

: "${BENCH_DIR:?BENCH_DIR is required}" 2>/dev/null || { usage >&2; exit 2; }
[[ -n "${ADMIN_PASSWORD:-}" ]] || { echo "ADMIN_PASSWORD is required" >&2; usage >&2; exit 2; }
SITE="${SITE:-saas-test.localhost}"
APP_PATH="${APP_PATH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

cd "$BENCH_DIR"
if [[ ! -d apps/saas_core ]]; then
  bench get-app saas_core "$APP_PATH"
fi
if [[ ! -d "sites/$SITE" ]]; then
  bench new-site "$SITE" --admin-password "$ADMIN_PASSWORD"
fi
bench --site "$SITE" list-apps | grep -q "^saas_core" || bench --site "$SITE" install-app saas_core
bench --site "$SITE" set-config allow_tests true
bench --site "$SITE" run-tests --app saas_core
