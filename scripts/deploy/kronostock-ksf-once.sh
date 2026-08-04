#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly APP_DIR="/srv/agent-workspaces/KronosStock"
readonly ENV_FILE="$APP_DIR/.env"
readonly LEDGER_DB="/srv/kronostock/data/ksf_ledger.sqlite3"

cd "$APP_DIR"
if ! [ -f "$ENV_FILE" ]; then
  echo "Missing required private env file: $ENV_FILE" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

# 러너가 원장 사이드카 잠금("$LEDGER_DB".lock)을 내부에서 직접 배타 획득한다
# (ksf/ledger_lock.py — 스냅샷 헬퍼와 동일 잠금으로 상호 배제). 외부 셸 잠금
# 래핑을 다시 추가하지 말 것: 내부 잠금과 이중화되어 자기교착 위험만 생긴다.
exec .venv/bin/python -m ksf.production_runner --db "$LEDGER_DB"
