"""원장 writer-배제 advisory lock (Linux fcntl.flock, fail-closed).

프로덕션 원장의 모든 writer(ksf.production_runner)와 대시보드 스냅샷 헬퍼는
DB 경로에서 유도되는 단 하나의 정식 잠금 파일 — Path(f"{db_path}.lock") — 위의
배타 flock 으로 직렬화된다. 잠금 경로를 호출자가 지정할 수 없게 해서, 직접
runner 를 호출해도 다른 잠금을 몰래 쓰는 일이 불가능하다.

- writer 모드: 잠금 파일이 없으면 안전하게 생성(O_CREAT|O_RDWR, 0600)한다.
- snapshot 모드: 이미 존재하는 잠금만 쓰기 권한 없이 연다(O_RDONLY, O_CREAT 금지).
  읽기 전용 fd 위의 배타 flock 은 Linux 에서 유효하다.
- 두 모드 모두 O_NOFOLLOW 로 열어 fstat 으로 검증한다: 정규 파일, 현재 유효 UID
  소유, group/world 권한 없음. 위반 시 파일을 고치거나 덮어쓰지 않고 실패한다.
- 대기는 monotonic 시계 기반의 논블로킹 flock 루프로 상한(기본 60초)을 두며,
  초과 시 fail-closed 다. 오류 메시지에 파일시스템 경로를 포함하지 않는다.
"""
from __future__ import annotations

import fcntl
import math
import os
import stat
import time
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 0.05
# O_NONBLOCK: FIFO 같은 비정규 파일에서 open 자체가 멈추는 것을 막는다(fstat 이 거른다).
_BASE_FLAGS = (
    os.O_CLOEXEC
    | os.O_NONBLOCK
    | getattr(os, "O_NOFOLLOW", 0)
)


class LedgerLockError(RuntimeError):
    """잠금을 안전하게 획득할 수 없는 상태 (fail-closed)."""


def lock_path_for(db_path: str | Path) -> Path:
    """정식 잠금 경로 — 항상 DB 경로에서 유도한다."""
    return Path(f"{Path(db_path)}.lock")


def _expected_owner_uid() -> int:
    return os.geteuid()


class LedgerLock:
    """획득된 잠금. release() 또는 컨텍스트 종료 시까지 배타 flock 을 유지한다."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # fd 종료가 flock 도 해제한다
            self._fd = None

    def __enter__(self) -> "LedgerLock":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _validate_pinned_fd(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise LedgerLockError("ledger lock is not a regular file")
    if info.st_uid != _expected_owner_uid():
        raise LedgerLockError("ledger lock is not owned by the expected user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise LedgerLockError("ledger lock permits group or world access")


def _flock_bounded(fd: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except (BlockingIOError, InterruptedError):
            if time.monotonic() >= deadline:
                raise LedgerLockError("timed out waiting for the ledger lock") from None
            time.sleep(_POLL_INTERVAL_SECONDS)


def _validate_timeout(timeout: float) -> float:
    # NaN 은 데드라인 비교를 전부 False 로 만들어 대기 루프가 무한히 돌고,
    # ±inf/음수는 '유한 상한 대기' 계약을 깬다 — 잠금 파일을 만들거나 열기 전에
    # 즉시 fail-closed 한다.
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        raise LedgerLockError("ledger lock timeout must be a finite nonnegative number") from None
    if not math.isfinite(value) or value < 0:
        raise LedgerLockError("ledger lock timeout must be a finite nonnegative number")
    return value


def _acquire(db_path: str | Path, *, flags: int, timeout: float) -> LedgerLock:
    timeout = _validate_timeout(timeout)
    try:
        fd = os.open(lock_path_for(db_path), flags, 0o600)
    except OSError as error:
        detail = error.strerror or type(error).__name__
        raise LedgerLockError(f"cannot open the ledger lock file: {detail}") from error
    try:
        _validate_pinned_fd(fd)
        _flock_bounded(fd, timeout)
    except BaseException:
        os.close(fd)
        raise
    return LedgerLock(fd)


def acquire_writer_lock(db_path: str | Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> LedgerLock:
    """writer 모드: 없으면 0600 으로 생성하고, DB open/마이그레이션/커밋/close 전
    구간 동안 보유해야 한다."""
    return _acquire(db_path, flags=_BASE_FLAGS | os.O_CREAT | os.O_RDWR, timeout=timeout)


def acquire_snapshot_lock(db_path: str | Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> LedgerLock:
    """snapshot 모드: 이미 존재하는 잠금만 읽기 전용으로 연다. 잠금 파일이 없으면
    생성하지 않고 실패한다 (배포 선행조건)."""
    return _acquire(db_path, flags=_BASE_FLAGS | os.O_RDONLY, timeout=timeout)
