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
: "${PAPER_AGENT_CYCLE_HANDOFF_PATH:?required}"
: "${PAPER_AGENT_PYTHON:=/usr/bin/python3}"

[[ "${PAPER_AGENT_PYTHON}" = /* ]]
python_real="$(readlink -f -- "${PAPER_AGENT_PYTHON}")"
if [[ "${python_real}" == /home || "${python_real}" == /home/* ]]; then
  printf '%s\n' 'paper-agent Python must not resolve through /home' >&2
  exit 1
fi
[[ -x "${PAPER_AGENT_PYTHON}" ]]

[[ "${PAPER_AGENT_CYCLE_HANDOFF_PATH}" = /* ]]
[[ -f "${PAPER_AGENT_CYCLE_HANDOFF_PATH}" && ! -L "${PAPER_AGENT_CYCLE_HANDOFF_PATH}" ]]
[[ "$(stat -c '%a' -- "${PAPER_AGENT_CYCLE_HANDOFF_PATH}")" == "600" ]]
[[ "$(stat -c '%u' -- "${PAPER_AGENT_CYCLE_HANDOFF_PATH}")" == "$(id -u)" ]]
[[ "$(stat -c '%s' -- "${PAPER_AGENT_CYCLE_HANDOFF_PATH}")" == "81" ]]
mapfile -t handoff_lines < "${PAPER_AGENT_CYCLE_HANDOFF_PATH}"
[[ "${#handoff_lines[@]}" == 2 ]]
[[ "${handoff_lines[0]}" =~ ^PAPER_AGENT_SESSION_ID=([0-9]{4})-([0-9]{2})-([0-9]{2})$ ]]
session_id="${handoff_lines[0]#PAPER_AGENT_SESSION_ID=}"
year="${BASH_REMATCH[1]}"; month="${BASH_REMATCH[2]}"; day="${BASH_REMATCH[3]}"
[[ "${handoff_lines[1]}" =~ ^PAPER_AGENT_CYCLE_AT=([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\+09:00$ ]]
cycle_at="${handoff_lines[1]#PAPER_AGENT_CYCLE_AT=}"
[[ "${BASH_REMATCH[1]}" == "${session_id}" ]]
hour="${BASH_REMATCH[2]}"; minute="${BASH_REMATCH[3]}"; second="${BASH_REMATCH[4]}"

# Validate the calendar and clock fields without invoking date or Python.
(( 10#${month} >= 1 && 10#${month} <= 12 ))
month_days=(0 31 28 31 30 31 30 31 31 30 31 30 31)
if (( 10#${year} % 400 == 0 || (10#${year} % 4 == 0 && 10#${year} % 100 != 0) )); then
  month_days[2]=29
fi
(( 10#${day} >= 1 && 10#${day} <= month_days[10#${month}] ))
(( 10#${hour} <= 23 && 10#${minute} <= 59 && 10#${second} <= 59 ))
bundle_path="${PAPER_AGENT_BUNDLE_DIR%/}/${session_id}.json"

[[ "${PAPER_AGENT_BUNDLE_DIR}" = /* && "${PAPER_AGENT_KSF_DB_PATH}" = /* ]]
[[ -f "${PAPER_AGENT_KSF_DB_PATH}" && ! -L "${PAPER_AGENT_KSF_DB_PATH}" ]]
[[ -f "${bundle_path}" && ! -L "${bundle_path}" ]]

export PAPER_AGENT_SESSION_ID="${session_id}"
export PAPER_AGENT_CYCLE_AT="${cycle_at}"
export PAPER_AGENT_RESPONSE_BUNDLE_PATH="${bundle_path}"
exec "${PAPER_AGENT_PYTHON}" -m strategy.paper_cycle --once
