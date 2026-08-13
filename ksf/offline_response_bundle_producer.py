"""Fail-closed producer for immutable deterministic paper-shadow bundles."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping

from ksf.response_bundle import build_response_bundle
from paper_trading.ledger import LedgerError
from strategy.paper_source import load_ksf_sqlite


_MAX_PATH_BYTES = 4096
_MAX_SOURCE_BYTES = 1024 * 1024 * 1024
_SESSION = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_OFFLINE_RESPONSE = {"offline_deterministic_abstain_reason": "OFFLINE_SHADOW_MODE"}


class BundleProducerDisabled(ValueError):
    """The offline producer was not explicitly enabled."""


def _path(value: object, name: str) -> Path:
    if type(value) is not str or not value or "\x00" in value \
            or len(value.encode("utf-8")) > _MAX_PATH_BYTES:
        raise LedgerError(f"{name} path is malformed")
    path = Path(value)
    if not path.is_absolute():
        raise LedgerError(f"{name} path must be absolute")
    return path


def _sha256_regular(path: Path) -> str:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise OSError
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        try:
            digest, size = hashlib.sha256(), 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_SOURCE_BYTES:
                    raise OSError
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(fd)
    except OSError as exc:
        raise LedgerError("KSF source is unavailable") from exc


def _publish(directory: Path, final_name: str, raw: bytes) -> Path:
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise OSError
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise LedgerError("bundle output directory is unavailable") from exc
    temp_name = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".response-bundle-", suffix=".tmp", dir=directory)
        temp_name = Path(temp_path).name
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temp_name, final_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                follow_symlinks=False)
        except FileExistsError as exc:
            raise LedgerError("daily response bundle already exists") from exc
        os.unlink(temp_name, dir_fd=dir_fd)
        temp_name = None
        os.fsync(dir_fd)
        return directory / final_name
    finally:
        if temp_name is not None:
            try: os.unlink(temp_name, dir_fd=dir_fd)
            except FileNotFoundError: pass
        os.close(dir_fd)


def produce_from_env(env: Mapping[str, str]) -> Path:
    """Validate KSF lineage and atomically publish one daily offline bundle."""
    if env.get("PAPER_AGENT_BUNDLE_PRODUCER_ENABLED") != "true":
        raise BundleProducerDisabled("bundle producer must be explicitly enabled")
    required = ("PAPER_AGENT_BUNDLE_DIR", "PAPER_AGENT_KSF_DB_PATH",
        "PAPER_AGENT_HORIZON_DAYS", "PAPER_AGENT_SESSION_ID", "PAPER_AGENT_CYCLE_AT",
        "PAPER_AGENT_MODEL_PROVIDER", "PAPER_AGENT_MODEL_NAME")
    if any(not env.get(key) for key in required):
        raise LedgerError("bundle producer configuration is incomplete")
    source = _path(env["PAPER_AGENT_KSF_DB_PATH"], "source")
    output_dir = _path(env["PAPER_AGENT_BUNDLE_DIR"], "output")
    session_id, cycle_at = env["PAPER_AGENT_SESSION_ID"], env["PAPER_AGENT_CYCLE_AT"]
    try:
        horizon = int(env["PAPER_AGENT_HORIZON_DAYS"])
        parsed = datetime.fromisoformat(cycle_at)
        if (str(horizon) != env["PAPER_AGENT_HORIZON_DAYS"] or horizon not in {1, 5, 20}
                or not _SESSION.fullmatch(session_id) or parsed.utcoffset() != timedelta(hours=9)
                or not cycle_at.endswith("+09:00") or parsed.date().isoformat() != session_id):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise LedgerError("bundle producer configuration is malformed") from exc
    for key in ("PAPER_AGENT_MODEL_PROVIDER", "PAPER_AGENT_MODEL_NAME"):
        if type(env[key]) is not str or not env[key].strip() or len(env[key]) > 256:
            raise LedgerError("bundle producer identity is malformed")
    source_sha = _sha256_regular(source)
    lineages, _observations = load_ksf_sqlite(source, session_id=session_id,
        horizon_days=horizon, cycle_at=cycle_at, source_sha256=source_sha)
    if _sha256_regular(source) != source_sha:
        raise LedgerError("KSF source changed during bundle production")
    lineage = {item.symbol: {"ksf_run_id": item.ksf_run_id,
        "ksf_decision_id": item.ksf_decision_id,
        "feature_snapshot_sha256": item.feature_snapshot_sha256} for item in lineages}
    responses = {symbol: dict(_OFFLINE_RESPONSE) for symbol in lineage}
    bundle = build_response_bundle(session_id=session_id, cycle_at=cycle_at,
        source_artifact_sha256=source_sha, lineage=lineage, responses=responses,
        model_provider=env["PAPER_AGENT_MODEL_PROVIDER"], model_name=env["PAPER_AGENT_MODEL_NAME"])
    raw = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _publish(output_dir, session_id + ".json", raw)


def main() -> int:
    try:
        path = produce_from_env(os.environ)
    except BundleProducerDisabled:
        print("response-bundle status=disabled")
        return 0
    except Exception:
        print("response-bundle status=failed", file=sys.stderr)
        return 1
    print(f"response-bundle status=produced file={path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
