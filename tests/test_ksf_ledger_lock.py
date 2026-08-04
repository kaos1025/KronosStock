"""ksf/ledger_lock.py — 원장 writer-배제 advisory lock (Linux flock, fail-closed) 검증.

Codex closure B: immutable 폴백 TOCTOU 를 막으려면 모든 프로덕션 writer 와
스냅샷의 사이드카 관찰~백업~발행 전 구간이 동일한 사이드카 잠금 파일
(Path(f"{db_path}.lock"))으로 직렬화되어야 한다. 잠금 파일이 없거나(스냅샷 모드),
심볼릭 링크/비정규 파일이거나, 소유자가 다르거나, group/world 권한이 있거나,
대기 시간이 초과되면 fail-closed 다.
"""
from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from ksf.ledger_lock import (
    LedgerLockError,
    acquire_snapshot_lock,
    acquire_writer_lock,
    lock_path_for,
)

# root 는 파일 권한을 우회하므로 권한 기반 fail-closed 시나리오가 성립하지 않는다.
pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="requires non-root permission semantics")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.sqlite3"
    path.write_bytes(b"")
    return path


def _make_lock(db: Path, mode: int = 0o600) -> Path:
    lock = lock_path_for(db)
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(lock, mode)
    return lock


def test_lock_path_is_canonical_db_sibling(db: Path):
    assert lock_path_for(db) == Path(f"{db}.lock")
    assert lock_path_for(str(db)) == Path(f"{db}.lock")


def test_writer_creates_missing_lock_privately_and_holds_exclusive(db: Path):
    lock_file = lock_path_for(db)
    assert not lock_file.exists()
    with acquire_writer_lock(db, timeout=1.0):
        info = lock_file.stat()
        assert stat.S_ISREG(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.geteuid()
        with pytest.raises(LedgerLockError):
            acquire_writer_lock(db, timeout=0.2)
    # 해제 후에는 즉시 재획득 가능해야 한다.
    acquire_writer_lock(db, timeout=0.2).release()


def test_snapshot_mode_requires_existing_lock_and_never_creates_it(db: Path):
    with pytest.raises(LedgerLockError):
        acquire_snapshot_lock(db, timeout=0.2)
    assert not lock_path_for(db).exists()


def test_snapshot_lock_excludes_writer_via_read_only_descriptor(db: Path):
    _make_lock(db)
    with acquire_snapshot_lock(db, timeout=1.0):
        with pytest.raises(LedgerLockError):
            acquire_writer_lock(db, timeout=0.2)
    acquire_writer_lock(db, timeout=0.2).release()


def test_snapshot_lock_works_without_write_permission_on_lock_file(db: Path):
    # 읽기 전용 fd 위의 배타 flock 은 Linux 에서 유효하다 — 대시보드 네임스페이스의
    # ReadOnlyPaths 아래에서도 스냅샷이 writer 를 배제할 수 있어야 한다.
    _make_lock(db, mode=0o400)
    with acquire_snapshot_lock(db, timeout=1.0):
        pass
    # 반대로 writer 모드는 쓰기 열기가 필요하므로 0400 잠금에서는 실패한다.
    with pytest.raises(LedgerLockError):
        acquire_writer_lock(db, timeout=0.2)


def test_symlink_lock_fails_closed_in_both_modes_without_touching_target(db: Path, tmp_path: Path):
    target = tmp_path / "innocent-target"
    target.write_bytes(b"sentinel")
    os.chmod(target, 0o600)
    lock_path_for(db).symlink_to(target)
    with pytest.raises(LedgerLockError):
        acquire_writer_lock(db, timeout=0.2)
    with pytest.raises(LedgerLockError):
        acquire_snapshot_lock(db, timeout=0.2)
    assert target.read_bytes() == b"sentinel"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_non_regular_lock_fails_closed_in_both_modes(db: Path):
    os.mkfifo(lock_path_for(db), 0o600)
    with pytest.raises(LedgerLockError):
        acquire_writer_lock(db, timeout=0.2)
    with pytest.raises(LedgerLockError):
        acquire_snapshot_lock(db, timeout=0.2)


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o640, 0o620, 0o602])
def test_group_or_world_accessible_lock_fails_closed_without_repair(db: Path, mode: int):
    lock_file = _make_lock(db, mode=mode)
    with pytest.raises(LedgerLockError):
        acquire_writer_lock(db, timeout=0.2)
    with pytest.raises(LedgerLockError):
        acquire_snapshot_lock(db, timeout=0.2)
    # fail-closed: unsafe 잠금 파일을 몰래 고치거나(chmod) 덮어쓰지 않는다.
    assert stat.S_IMODE(lock_file.stat().st_mode) == mode


def test_foreign_owner_fails_closed(db: Path, monkeypatch: pytest.MonkeyPatch):
    import ksf.ledger_lock as ledger_lock

    _make_lock(db)
    monkeypatch.setattr(ledger_lock, "_expected_owner_uid", lambda: os.geteuid() + 1)
    with pytest.raises(LedgerLockError):
        acquire_writer_lock(db, timeout=0.2)
    with pytest.raises(LedgerLockError):
        acquire_snapshot_lock(db, timeout=0.2)


def test_missing_parent_directory_fails_closed(tmp_path: Path):
    with pytest.raises(LedgerLockError):
        acquire_writer_lock(tmp_path / "missing-dir" / "ledger.sqlite3", timeout=0.2)


@pytest.mark.parametrize("bad_timeout", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_invalid_timeout_fails_closed_before_touching_lock_file(db: Path, bad_timeout: float):
    # NaN 은 monotonic 데드라인 비교를 전부 False 로 만들어 대기 루프가 영원히 돌고,
    # ±inf/음수는 '유한 상한 대기' 계약을 깬다 — 잠금 파일을 만들거나 열기 전에
    # 즉시 fail-closed 해야 한다.
    with pytest.raises(LedgerLockError):
        acquire_writer_lock(db, timeout=bad_timeout)
    assert not lock_path_for(db).exists()
    _make_lock(db)
    with pytest.raises(LedgerLockError):
        acquire_snapshot_lock(db, timeout=bad_timeout)
    # 유효한 timeout 으로는 그대로 획득 가능해야 한다 (파일 상태 오염 없음).
    acquire_writer_lock(db, timeout=0.2).release()


def test_contention_times_out_within_bound_and_recovers(db: Path):
    _make_lock(db)
    holder = acquire_writer_lock(db, timeout=1.0)
    try:
        start = time.monotonic()
        with pytest.raises(LedgerLockError):
            acquire_snapshot_lock(db, timeout=0.5)
        elapsed = time.monotonic() - start
        assert 0.4 <= elapsed < 5.0
    finally:
        holder.release()
    acquire_snapshot_lock(db, timeout=0.2).release()


def test_waiter_blocks_then_acquires_after_release(db: Path):
    _make_lock(db)
    holder = acquire_writer_lock(db, timeout=1.0)
    acquired = threading.Event()

    def waiter():
        lock = acquire_writer_lock(db, timeout=10.0)
        acquired.set()
        lock.release()

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        time.sleep(0.3)
        assert not acquired.is_set()
    finally:
        holder.release()
        thread.join(timeout=10)
    assert acquired.is_set()


def test_error_messages_do_not_embed_filesystem_paths(db: Path):
    with pytest.raises(LedgerLockError) as missing:
        acquire_snapshot_lock(db, timeout=0.2)
    assert str(db.parent) not in str(missing.value)

    _make_lock(db)
    holder = acquire_writer_lock(db, timeout=1.0)
    try:
        with pytest.raises(LedgerLockError) as busy:
            acquire_writer_lock(db, timeout=0.2)
    finally:
        holder.release()
    assert str(db.parent) not in str(busy.value)
