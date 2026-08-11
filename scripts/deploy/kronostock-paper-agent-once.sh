#!/usr/bin/env bash
set -euo pipefail

# Repository wrapper only. The service remains disabled unless explicitly gated.
if [[ "${PAPER_AGENT_ENABLED:-}" != "true" ]]; then
  printf '%s\n' 'paper-cycle status=disabled'
  exit 0
fi
if [[ "${PAPER_AGENT_MODE:-}" != "shadow" && "${PAPER_AGENT_MODE:-}" != "fills" ]]; then
  printf '%s\n' 'paper-cycle status=failed' >&2
  exit 1
fi
if [[ "${PAPER_AGENT_MODE}" == "fills" && "${PAPER_AGENT_FILLS_ENABLED:-}" != "true" ]]; then
  printf '%s\n' 'paper-cycle status=failed' >&2
  exit 1
fi
: "${PAPER_AGENT_BUNDLE_DIR:?required}"
: "${PAPER_AGENT_KSF_DB_PATH:?required}"

cycle_at="$(date -u -d '+9 hours' '+%Y-%m-%dT%H:%M:%S+09:00')"
session_id="${cycle_at%%T*}"
bundle_path="${PAPER_AGENT_BUNDLE_DIR%/}/${session_id}.json"

[[ "${PAPER_AGENT_BUNDLE_DIR}" = /* && "${PAPER_AGENT_KSF_DB_PATH}" = /* ]]
[[ -f "${PAPER_AGENT_KSF_DB_PATH}" && ! -L "${PAPER_AGENT_KSF_DB_PATH}" ]]
[[ -f "${bundle_path}" && ! -L "${bundle_path}" ]]

export PAPER_AGENT_SESSION_ID="${session_id}"
export PAPER_AGENT_CYCLE_AT="${cycle_at}"
export PAPER_AGENT_RESPONSE_BUNDLE_PATH="${bundle_path}"
exec /srv/agent-workspaces/KronosStock/.venv/bin/python -m strategy.paper_cycle --once
