#!/usr/bin/env bash
# inference/vendor_kronos.sh
# Kronos 의 `model/` 패키지만 sparse-checkout 으로 받아 repo 루트에 vendoring 한다.
# predictor.py 의 `from model import Kronos, KronosTokenizer, KronosPredictor` 가
# 이 디렉터리를 import 한다.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[vendor] Cloning shiyu-coder/Kronos (sparse: model/) ..."
git clone --depth 1 --filter=blob:none --sparse \
    https://github.com/shiyu-coder/Kronos.git "$TMP/Kronos"
git -C "$TMP/Kronos" sparse-checkout set model

echo "[vendor] Copying model/ -> $REPO_ROOT/model"
rm -rf "$REPO_ROOT/model"
cp -r "$TMP/Kronos/model" "$REPO_ROOT/model"

echo "[vendor] Done."
echo "         vendored: $REPO_ROOT/model"
echo "         (Kronos 가중치는 최초 추론 시 HuggingFace Hub 에서 자동 다운로드됩니다.)"
