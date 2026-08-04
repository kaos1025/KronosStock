#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly APP_DIR="/srv/agent-workspaces/KronosStock"
readonly ENV_FILE="$APP_DIR/.env"
readonly LEDGER_DB="/srv/kronostock/data/ksf_ledger.sqlite3"
readonly LOCK_FILE="/run/lock/kronostock-ksf.lock"

cd "$APP_DIR"
if ! [ -f "$ENV_FILE" ]; then
  echo "Missing required private env file: $ENV_FILE" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

exec flock -n "$LOCK_FILE" .venv/bin/python -m ksf.production_runner --db "$LEDGER_DB"
