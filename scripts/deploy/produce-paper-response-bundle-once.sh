#!/usr/bin/env bash
set -euo pipefail

if [[ "${PAPER_AGENT_BUNDLE_PRODUCER_ENABLED:-}" != "true" ]]; then
  printf '%s\n' 'response-bundle status=disabled'
  exit 0
fi

: "${PAPER_AGENT_BUNDLE_DIR:?required}"
: "${PAPER_AGENT_KSF_DB_PATH:?required}"
: "${PAPER_AGENT_HORIZON_DAYS:?required}"
: "${PAPER_AGENT_MODEL_PROVIDER:?required}"
: "${PAPER_AGENT_MODEL_NAME:?required}"

cycle_at="$(date -u -d '+9 hours' '+%Y-%m-%dT%H:%M:%S+09:00')"
export PAPER_AGENT_SESSION_ID="${cycle_at%%T*}"
export PAPER_AGENT_CYCLE_AT="${cycle_at}"
exec /srv/agent-workspaces/KronosStock/.venv/bin/python -m ksf.offline_response_bundle_producer
