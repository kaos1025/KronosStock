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
handoff_dir="${PAPER_AGENT_CYCLE_HANDOFF_PATH%/*}"
handoff_name="${PAPER_AGENT_CYCLE_HANDOFF_PATH##*/}"
[[ -n "${handoff_name}" && -d "${handoff_dir}" && ! -L "${handoff_dir}" ]]

# Invalidate an earlier successful invocation before attempting this production.
if [[ -e "${PAPER_AGENT_CYCLE_HANDOFF_PATH}" || -L "${PAPER_AGENT_CYCLE_HANDOFF_PATH}" ]]; then
  [[ -f "${PAPER_AGENT_CYCLE_HANDOFF_PATH}" && ! -L "${PAPER_AGENT_CYCLE_HANDOFF_PATH}" ]]
  rm -- "${PAPER_AGENT_CYCLE_HANDOFF_PATH}"
fi

cycle_at="$(date -u -d '+9 hours' '+%Y-%m-%dT%H:%M:%S+09:00')"
export PAPER_AGENT_SESSION_ID="${cycle_at%%T*}"
export PAPER_AGENT_CYCLE_AT="${cycle_at}"
"${PAPER_AGENT_PYTHON}" -m ksf.offline_response_bundle_producer

handoff_tmp="$(mktemp "${handoff_dir}/.${handoff_name}.XXXXXX")"
cleanup() { rm -f -- "${handoff_tmp}"; }
trap cleanup EXIT
chmod 0600 "${handoff_tmp}"
printf 'PAPER_AGENT_SESSION_ID=%s\nPAPER_AGENT_CYCLE_AT=%s\n' \
  "${PAPER_AGENT_SESSION_ID}" "${PAPER_AGENT_CYCLE_AT}" > "${handoff_tmp}"
mv -fT -- "${handoff_tmp}" "${PAPER_AGENT_CYCLE_HANDOFF_PATH}"
trap - EXIT
