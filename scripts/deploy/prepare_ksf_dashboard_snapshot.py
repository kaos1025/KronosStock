#!/usr/bin/env python3
"""KSF 대시보드 서비스 시작용 원장 스냅샷 생성기 (systemd ExecStartPre 전용).

프로덕션 원장(/srv/kronostock/data/ksf_ledger.sqlite3)은 의도적으로 WAL 저널
모드이며, 대시보드 서비스 네임스페이스에서는 완전 읽기 전용이다. WAL DB 는
사이드카(-wal/-shm)가 없으면 읽기 전용 트리에서 mode=ro 로도 열리지 않으므로
("attempt to write a readonly database"), 서비스 시작 시점에 런타임 디렉터리
(/run/kronostock-dashboard)로 point-in-time 스냅샷을 만들어 대시보드가 그것만
읽게 한다. 스냅샷은 DELETE 저널 모드라 사이드카 없이 읽기 전용으로 열린다.

원본 접근 규칙 (커밋 유실 금지):
- 모든 관찰/백업은 원장 사이드카 잠금 `<ledger>.lock` (ksf.ledger_lock, 배타
  flock) 을 쥔 상태에서만 수행한다. 프로덕션 writer(ksf.production_runner)도
  같은 잠금을 run 전체 동안 내부적으로 쥐므로, 사이드카 관찰 시점과 백업 시점
  사이에 writer 가 시작/커밋하는 TOCTOU (Codex blocker B) 가 차단된다.
- 정상 경로: mode=ro + query_only — 읽을 수 있는 -wal/-shm 이 있으면 아직
  체크포인트되지 않은 커밋까지 포함해서 읽는다.
- immutable=1 폴백: -wal 과 -shm 이 **둘 다 없을 때만** 사용한다. 사이드카
  부재 관찰만으로는 안전이 보장되지 않는다 — 관찰 직후 writer 가 시작해 커밋할
  수 있기 때문이다. 폴백이 안전한 것은 오직 writer-배제 잠금을 관찰~백업~발행
  전 구간 동안 쥐고 있는 동안뿐이다. 사이드카가 하나라도 있으면 immutable 은
  커밋된 WAL 프레임을 무시할 수 있어 절대 쓰지 않는다(fail-closed).
- 잠금 파일은 배포 선행조건이다: 이 헬퍼는 O_CREAT 없이 읽기 전용으로만 열며,
  잠금이 없거나(unsafe 포함) 대기 상한(60초)을 넘기면 서비스 시작을 막는다.

프로덕션 원장/사이드카는 절대 변경하지 않는다. 실패 시 임시 파일을 정리하고
비정상 종료해 서비스 시작을 막는다.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

# systemd ExecStartPre 는 이 파일을 스크립트로 직접 실행한다 — repo 루트를 올려
# 공유 잠금 모듈(ksf.ledger_lock)을 정확히 재사용한다(잠금 경로 유도 로직 단일화).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ksf.ledger_lock import LedgerLockError, acquire_snapshot_lock  # noqa: E402


class SnapshotError(RuntimeError):
    """스냅샷을 안전하게 만들 수 없는 상태 (fail-closed)."""


def _source_uri(source: Path, *, immutable: bool) -> str:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    return f"file:{quote(str(source), safe='/')}?{query}"


def _copy_into(source: Path, temp_path: Path, *, immutable: bool) -> None:
    src = sqlite3.connect(_source_uri(source, immutable=immutable), uri=True)
    try:
        src.execute("PRAGMA query_only = ON")
        dst = sqlite3.connect(temp_path)
        try:
            src.backup(dst)
            check = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise SnapshotError(f"snapshot integrity_check failed: {check}")
            # backup 은 페이지 단위 복사라 헤더의 WAL 플래그까지 따라온다 —
            # 읽기 전용 트리에서 사이드카 없이 열리도록 DELETE 저널로 확정한다.
            mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode != "delete":
                raise SnapshotError(f"snapshot journal_mode conversion failed: {mode}")
        finally:
            dst.close()
    finally:
        src.close()


def _cleanup_temp(temp_path: Path) -> None:
    for path in (temp_path, *(Path(f"{temp_path}{suffix}") for suffix in ("-journal", "-wal", "-shm"))):
        path.unlink(missing_ok=True)


def create_snapshot(source: Path, destination: Path, *, _after_lock_acquired=None) -> Path:
    """원장의 point-in-time 스냅샷을 원자적으로 발행한다.

    `_after_lock_acquired` 는 테스트 전용 내부 seam 이다(키워드 전용, 잠금 획득
    직후·관찰 이전에만 호출). 프로덕션 경로/환경변수로는 노출되지 않는다.
    """
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise SnapshotError(f"source ledger not found: {source}")
    if not destination.parent.is_dir():
        raise SnapshotError(f"destination directory missing (RuntimeDirectory?): {destination.parent}")
    if Path(os.path.realpath(source)) == Path(os.path.realpath(destination)):
        raise SnapshotError(f"source and destination must be distinct: {source}")

    # writer-배제 잠금을 사이드카 관찰 전에 획득하고 발행까지 계속 쥔다.
    # 잠금이 없거나 unsafe 하거나 대기 상한을 넘기면 fail-closed.
    try:
        lock = acquire_snapshot_lock(source)
    except LedgerLockError as error:
        raise SnapshotError(f"writer-exclusion ledger lock unavailable (fail-closed): {error}") from error
    try:
        if _after_lock_acquired is not None:
            _after_lock_acquired()

        wal_exists = Path(f"{source}-wal").exists()
        shm_exists = Path(f"{source}-shm").exists()
        if wal_exists != shm_exists:
            raise SnapshotError(
                "exactly one WAL sidecar exists next to the source ledger — "
                "cannot prove all committed frames are visible, refusing to snapshot"
            )

        fd, temp_name = tempfile.mkstemp(prefix=f"{destination.name}.", suffix=".tmp", dir=destination.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            try:
                _copy_into(source, temp_path, immutable=False)
            except sqlite3.Error as error:
                if wal_exists or shm_exists:
                    raise SnapshotError(
                        f"read-only open of WAL source failed while sidecars exist — "
                        f"refusing immutable fallback (would ignore committed WAL frames): {error}"
                    ) from error
                # 사이드카가 둘 다 없고 **잠금을 쥐고 있으므로** 새 writer 가 그 사이
                # 시작/커밋할 수 없다 — 이때만 immutable 이 안전하다.
                temp_path.write_bytes(b"")
                _copy_into(source, temp_path, immutable=True)
            os.chmod(temp_path, 0o400)
            os.replace(temp_path, destination)
        except BaseException:
            _cleanup_temp(temp_path)
            raise
    finally:
        lock.release()
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prepare_ksf_dashboard_snapshot",
        description="Create a point-in-time read-only snapshot of the KSF ledger for the dashboard service.",
    )
    parser.add_argument("source", type=Path, help="production ledger (read-only, never modified)")
    parser.add_argument("destination", type=Path, help="runtime snapshot path (e.g. /run/kronostock-dashboard/ksf-ledger.sqlite3)")
    args = parser.parse_args(argv)
    try:
        create_snapshot(args.source, args.destination)
    except (SnapshotError, sqlite3.Error, OSError) as error:
        print(f"prepare_ksf_dashboard_snapshot: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
