"""KSF 대시보드 서비스 시작 스냅샷 헬퍼 (scripts/deploy/prepare_ksf_dashboard_snapshot.py) 검증.

Hermes blocker B 재현 기반:
- WAL 원장은 사이드카(-wal/-shm)가 없으면 완전 읽기 전용 디렉터리에서 mode=ro 로 열리지 않는다.
- 헬퍼는 프로덕션 원장을 절대 변경하지 않고 런타임 디렉터리에 point-in-time
  스냅샷(DELETE 저널)을 원자적으로 만든다.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from ksf.ledger_lock import DEFAULT_TIMEOUT_SECONDS, LedgerLockError, acquire_writer_lock

# pytest 가 tests/ 를 sys.path 에 넣으므로 실제 runner 용 fake collector 의존성을
# 그대로 재사용한다 (레이스 B/C 는 실제 run_once writer 경로를 요구한다).
from test_ksf_production_runner import AS_OF, fake_domestic, fake_global, fake_price

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "deploy" / "prepare_ksf_dashboard_snapshot.py"
)

# root 는 파일 권한을 우회하므로 읽기 전용 트리 시나리오가 성립하지 않는다.
pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="requires non-root permission semantics")


def _load_helper():
    spec = importlib.util.spec_from_file_location("prepare_ksf_dashboard_snapshot", SCRIPT_PATH)
    assert spec and spec.loader, f"snapshot helper missing: {SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


def _run_helper(source: Path, destination: Path) -> subprocess.CompletedProcess:
    """프로덕션과 동일하게 별도 프로세스로 실행한다.

    같은 프로세스 안에서 부르면 테스트 writer 연결이 열어 둔 쓰기 가능 -shm
    매핑을 SQLite 가 재사용해 읽기 전용 시나리오가 성립하지 않는다.
    """
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(source), str(destination)],
        capture_output=True, text=True, timeout=60,
    )


def _sidecars(source: Path) -> tuple[Path, Path]:
    return Path(f"{source}-wal"), Path(f"{source}-shm")


def _lock_path(source: Path) -> Path:
    return Path(f"{source}.lock")


def _create_lock(source: Path) -> Path:
    """배포 선행조건과 동일한 writer-배제 잠금 파일 (0600, 현재 사용자 소유)."""
    lock = _lock_path(source)
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    return lock


def _create_wal_source(source: Path, *, close: bool) -> sqlite3.Connection | None:
    """WAL 원장 생성. close=False 면 커밋된 미체크포인트 행이 -wal 에만 존재한다."""
    conn = sqlite3.connect(source)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE ledger_rows (label TEXT NOT NULL)")
    conn.execute("INSERT INTO ledger_rows VALUES ('checkpointed-row')")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("INSERT INTO ledger_rows VALUES ('uncheckpointed-row')")
    conn.commit()
    if close:
        conn.close()  # 마지막 연결 종료 → 최종 체크포인트 + 사이드카 삭제
        return None
    return conn


@contextmanager
def _read_only_tree(root: Path, *, unreadable: tuple[Path, ...] = ()):
    try:
        for path in root.iterdir():
            if path in unreadable:
                path.chmod(0o000)
            elif path.name.endswith(".lock"):
                # 프로덕션에서도 잠금 파일은 0600 owner 소유를 유지한다 — 데이터
                # 트리가 읽기 전용이어도 flock 은 fd 만 있으면 걸 수 있다.
                path.chmod(0o600)
            else:
                path.chmod(0o444)
        root.chmod(0o555)
        yield
    finally:
        root.chmod(0o755)
        for path in root.iterdir():
            path.chmod(0o600 if path.name.endswith(".lock") else 0o644)


def _snapshot_rows(destination: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute("SELECT label FROM ledger_rows")}
    finally:
        conn.close()


def _hashes(*paths: Path) -> list[str]:
    return [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    source = source_dir / "ksf_ledger.sqlite3"
    _create_lock(source)
    return source, source_dir, run_dir


def test_live_wal_source_snapshots_uncheckpointed_commit_from_read_only_tree(tree):
    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    writer = _create_wal_source(source, close=False)
    try:
        wal, shm = _sidecars(source)
        assert wal.exists() and shm.exists()
        with _read_only_tree(source_dir):
            result = _run_helper(source, destination)
        assert result.returncode == 0, result.stderr
        # 커밋됐지만 아직 체크포인트되지 않은 행이 스냅샷에 포함되어야 한다.
        assert _snapshot_rows(destination) == {"checkpointed-row", "uncheckpointed-row"}
    finally:
        writer.close()


def test_cleanly_closed_wal_source_snapshots_via_immutable_fallback(tree):
    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    _create_wal_source(source, close=True)
    wal, shm = _sidecars(source)
    assert not wal.exists() and not shm.exists()
    with _read_only_tree(source_dir):
        # Hermes 재현 전제조건: 사이드카 없는 WAL DB 는 mode=ro 로 열어 읽을 수 없다.
        probe = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                probe.execute("SELECT count(*) FROM ledger_rows").fetchone()
        finally:
            probe.close()
        helper.create_snapshot(source, destination)
    assert _snapshot_rows(destination) == {"checkpointed-row", "uncheckpointed-row"}


def test_exactly_one_sidecar_fails_closed_without_artifacts(tree):
    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    _create_wal_source(source, close=True)
    wal, _ = _sidecars(source)
    wal.touch()  # -shm 없이 -wal 만 존재 → 판단 불가, immutable 금지
    with _read_only_tree(source_dir):
        with pytest.raises(helper.SnapshotError):
            helper.create_snapshot(source, destination)
    assert not destination.exists()
    assert list(run_dir.iterdir()) == []


def test_open_failure_with_sidecars_present_fails_closed_without_artifacts(tree):
    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    writer = _create_wal_source(source, close=False)
    try:
        wal, shm = _sidecars(source)
        assert wal.exists() and shm.exists()
        # -shm 을 읽을 수 없으면 정상 mode=ro 열기가 실패한다. 이때 immutable 로
        # 재시도하면 -wal 의 커밋을 무시하게 되므로 반드시 실패해야 한다.
        with _read_only_tree(source_dir, unreadable=(shm,)):
            result = _run_helper(source, destination)
        assert result.returncode == 1, result.stdout
        assert "refusing immutable fallback" in result.stderr
        assert not destination.exists()
        assert list(run_dir.iterdir()) == []
    finally:
        writer.close()


def test_snapshot_is_private_delete_journal_and_readable_from_read_only_runtime_dir(tree):
    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    writer = _create_wal_source(source, close=False)
    try:
        with _read_only_tree(source_dir):
            result = _run_helper(source, destination)
        assert result.returncode == 0, result.stderr
    finally:
        writer.close()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o400
    run_dir.chmod(0o500)
    try:
        conn = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert {row[0] for row in conn.execute("SELECT label FROM ledger_rows")} == {
                "checkpointed-row", "uncheckpointed-row",
            }
        finally:
            conn.close()
    finally:
        run_dir.chmod(0o700)


def test_source_main_wal_shm_bytes_are_unchanged_by_snapshot(tree):
    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    writer = _create_wal_source(source, close=False)
    try:
        wal, shm = _sidecars(source)
        before = _hashes(source, wal, shm)
        with _read_only_tree(source_dir):
            result = _run_helper(source, destination)
            during = _hashes(source, wal, shm)
        assert result.returncode == 0, result.stderr
        assert during == before
    finally:
        writer.close()


def test_rejects_identical_paths_and_missing_destination_directory(tree):
    source, source_dir, run_dir = tree
    _create_wal_source(source, close=True)
    with pytest.raises(helper.SnapshotError):
        helper.create_snapshot(source, source)
    with pytest.raises(helper.SnapshotError):
        helper.create_snapshot(source, run_dir / "missing-subdir" / "snapshot.sqlite3")
    with pytest.raises(helper.SnapshotError):
        helper.create_snapshot(source_dir / "no-such.sqlite3", run_dir / "snapshot.sqlite3")


# --- Codex closure B: writer-배제 잠금 (모든 관찰/백업 구간을 flock 으로 방호) ---


def test_missing_writer_exclusion_lock_fails_closed_without_artifacts(tree):
    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    _create_wal_source(source, close=True)
    _lock_path(source).unlink()
    with pytest.raises(helper.SnapshotError):
        helper.create_snapshot(source, destination)
    # 스냅샷 모드는 잠금 파일을 절대 생성하지 않는다 (O_CREAT 금지).
    assert not _lock_path(source).exists()
    assert not destination.exists()
    assert list(run_dir.iterdir()) == []


def test_group_or_world_accessible_lock_fails_closed_without_artifacts(tree):
    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    _create_wal_source(source, close=True)
    _lock_path(source).chmod(0o644)
    with pytest.raises(helper.SnapshotError):
        helper.create_snapshot(source, destination)
    # fail-closed: unsafe 잠금을 고치거나 덮어쓰지 않는다.
    assert stat.S_IMODE(_lock_path(source).stat().st_mode) == 0o644
    assert list(run_dir.iterdir()) == []


def test_symlink_lock_fails_closed_without_artifacts(tree, tmp_path: Path):
    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    _create_wal_source(source, close=True)
    _lock_path(source).unlink()
    decoy = tmp_path / "decoy.lock"
    decoy.write_bytes(b"")
    os.chmod(decoy, 0o600)
    _lock_path(source).symlink_to(decoy)
    with pytest.raises(helper.SnapshotError):
        helper.create_snapshot(source, destination)
    assert list(run_dir.iterdir()) == []


def test_snapshot_waits_for_writer_lock_release_and_includes_final_commit(tree):
    """레이스 A: writer 가 정식 잠금을 쥔 채 미체크포인트 커밋을 만드는 동안 시작된
    스냅샷은 잠금 해제 전에는 관찰/백업 단계에 진입할 수 없고, 해제 후에는 그
    커밋을 포함해야 한다."""
    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    writer = _create_wal_source(source, close=False)
    lock_fd = os.open(_lock_path(source), os.O_RDWR)
    proc: subprocess.Popen | None = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH), str(source), str(destination)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(1.5)
        # writer 가 잠금을 쥔 동안 helper 는 종료되지 않았고 어떤 산출물도 없다.
        assert proc.poll() is None, (proc.stdout.read(), proc.stderr.read())
        assert not destination.exists()
        assert list(run_dir.iterdir()) == []
        # 잠금을 쥔 채로 커밋 (wal_autocheckpoint=0 → -wal 에만 존재).
        writer.execute("INSERT INTO ledger_rows VALUES ('post-lock-row')")
        writer.commit()
        writer.close()
        writer = None
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, stderr
        assert _snapshot_rows(destination) == {
            "checkpointed-row", "uncheckpointed-row", "post-lock-row",
        }
    finally:
        os.close(lock_fd)
        if writer is not None:
            writer.close()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()


def _paused_snapshot_thread(
    source: Path,
    destination: Path,
    snapshot_locked: threading.Event,
    release_snapshot: threading.Event,
    errors: list,
) -> threading.Thread:
    """실제 acquire_snapshot_lock 획득 직후(관찰 이전)에 멈추는 스냅샷 스레드.

    `_after_lock_acquired` 는 create_snapshot 의 테스트 전용 private seam 이다 —
    잠금을 쥔 채로 멈추므로, 그동안 실제 writer 잠금 경쟁을 결정적으로 관찰할 수 있다.
    """

    def run() -> None:
        def hold() -> None:
            snapshot_locked.set()
            assert release_snapshot.wait(timeout=30)

        try:
            helper.create_snapshot(source, destination, _after_lock_acquired=hold)
        except BaseException as error:  # noqa: BLE001 — 스레드 밖에서 재검사
            errors.append(error)

    return threading.Thread(target=run)


def _observe_runner_lock_acquisition(
    monkeypatch: pytest.MonkeyPatch, writer_at_lock: threading.Event
) -> None:
    """run_once 가 **실제 잠금 획득 시도에 도달한 순간**을 이벤트로 노출한다.

    ksf.production_runner 에 바인딩된 acquire_writer_lock 이름만 감싸고, 획득
    자체는 그대로 실제 ksf.ledger_lock.acquire_writer_lock 에 위임한다 — 실제
    flock 경로가 변조 없이 실행된다. run_once 에서 acquire_writer_lock 호출이
    빠지면(mutation) 이 이벤트는 영원히 set 되지 않으므로, '스레드가 단순히
    스케줄되지 않았다'와 '잠금에 도달했지만 막혔다'를 결정적으로 구분한다."""
    import ksf.production_runner as production_runner

    def observed_acquire_writer_lock(db_path, *, timeout=DEFAULT_TIMEOUT_SECONDS):
        writer_at_lock.set()
        return acquire_writer_lock(db_path, timeout=timeout)

    monkeypatch.setattr(production_runner, "acquire_writer_lock", observed_acquire_writer_lock)


def test_race_b_paused_snapshot_lock_blocks_real_runner_writer_until_release(
    tree, monkeypatch: pytest.MonkeyPatch
):
    """레이스 B: 스냅샷이 **실제** acquire_snapshot_lock 을 쥔 채 멈춰 있는 동안,
    **실제** ksf.production_runner.run_once writer 는 잠금에 막혀 어떤 stage 에도
    진입하지 못하고, 스냅샷 발행·잠금 해제 후에만 진행한다.

    mutation-sensitive: create_snapshot 에서 acquire_snapshot_lock 을 빼면 직접
    acquire_writer_lock 검사가 즉시 성공해 실패하고, run_once 에서
    acquire_writer_lock 을 빼면 writer_at_lock 이벤트가 영원히 set 되지 않아
    결정적으로 실패한다 (스케줄링 지연으로는 통과할 수 없다)."""
    from ksf.production_runner import RunnerDependencies, run_once

    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"

    # 실제 runner 로 원장을 만든다 (migrations → WAL, close 시 checkpoint 로 사이드카 소멸).
    setup_deps = RunnerDependencies(
        domestic=fake_domestic, domestic_price=fake_price, global_collect=fake_global, now=lambda: AS_OF
    )
    assert run_once(source, dependencies=setup_deps)["status"] == "ok"
    assert not any(path.exists() for path in _sidecars(source))

    # setup run 이후에 계측을 설치한다 — 이벤트는 racing writer 의 시도만 기록한다.
    writer_at_lock = threading.Event()
    _observe_runner_lock_acquisition(monkeypatch, writer_at_lock)

    snapshot_locked = threading.Event()
    release_snapshot = threading.Event()
    snapshot_errors: list[BaseException] = []
    snapshot_thread = _paused_snapshot_thread(
        source, destination, snapshot_locked, release_snapshot, snapshot_errors
    )

    writer_in_domestic = threading.Event()
    destination_seen_at_domestic: list[bool] = []

    def racing_domestic(conn, *, symbols, as_of_kst):
        # 발행(os.replace) → 잠금 해제 → writer 진입 순서라면 여기서 스냅샷은 이미 존재한다.
        destination_seen_at_domestic.append(destination.exists())
        writer_in_domestic.set()
        return fake_domestic(conn, symbols=symbols, as_of_kst=as_of_kst)

    writer_results: list[dict] = []
    writer_errors: list[BaseException] = []
    writer_deps = RunnerDependencies(
        domestic=racing_domestic, domestic_price=fake_price, global_collect=fake_global, now=lambda: AS_OF
    )

    def writer() -> None:
        try:
            writer_results.append(run_once(source, dependencies=writer_deps))
        except BaseException as error:  # noqa: BLE001 — 스레드 밖에서 재검사
            writer_errors.append(error)

    writer_thread = threading.Thread(target=writer)
    snapshot_thread.start()
    try:
        assert snapshot_locked.wait(timeout=30)
        # 스냅샷이 실제 사이드카 잠금을 쥐고 있음을 직접 증명한다 —
        # acquire_snapshot_lock 이 빠지면 이 획득이 성공해 테스트가 실패한다.
        with pytest.raises(LedgerLockError):
            acquire_writer_lock(source, timeout=0.2)
        writer_thread.start()
        # writer 가 **실제 잠금 획득 시도**(acquire_writer_lock 진입)에 도달했음을
        # 먼저 결정적으로 증명한다 — run_once 의 acquire_writer_lock 이 빠지면
        # 이 이벤트가 set 되지 않아 여기서 실패한다.
        assert writer_at_lock.wait(timeout=30)
        # 잠금 시도에 도달한 writer 는 스냅샷이 쥔 배타 flock 에 막혀 DB 를
        # 열지도, stage 에 진입하지도 못한다 (위 증명 덕에 '미스케줄' 오탐 불가).
        assert not writer_in_domestic.wait(timeout=1.0)
        assert not destination.exists()
    finally:
        release_snapshot.set()
        snapshot_thread.join(timeout=60)
        if writer_thread.ident is not None:
            writer_thread.join(timeout=60)

    assert snapshot_errors == []
    assert writer_errors == []
    assert [summary["status"] for summary in writer_results] == ["ok"]
    # writer 는 스냅샷이 발행된 뒤에만 stage 에 진입했다.
    assert destination_seen_at_domestic == [True]
    conn = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM ksf_schema_versions").fetchone()[0] == 2
    finally:
        conn.close()


def test_race_c_immutable_fallback_is_pre_writer_and_real_writer_commits_only_after_release(
    tree, monkeypatch: pytest.MonkeyPatch
):
    """레이스 C: 사이드카가 둘 다 없어 immutable 폴백으로 스냅샷을 만드는 동안
    **실제** run_once writer 는 잠금에 막힌다. 스냅샷은 writer 커밋 이전(pre-writer)
    상태만 담고, writer 는 스냅샷 발행·잠금 해제 후에야 원본에 커밋한다.

    mutation-sensitive: 스냅샷 잠금이 빠지면 직접 acquire_writer_lock 검사가
    실패하고, run_once 의 writer 잠금이 빠지면 writer_at_lock 이벤트가 영원히
    set 되지 않아 결정적으로 실패한다 (스케줄링 지연으로는 통과할 수 없다)."""
    from ksf.production_runner import RunnerDependencies, run_once

    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    _create_wal_source(source, close=True)
    assert not any(path.exists() for path in _sidecars(source))

    # 디렉터리만 쓰기 불가로 만들어 mode=ro(-shm 생성 필요)를 실패시키고 immutable
    # 폴백을 강제한다. 원장 파일 자체는 소유자 쓰기 가능이라, 잠금 해제 후 실제
    # writer 가 그대로 커밋할 수 있다 (run_once 가 부모 디렉터리를 0700 으로 되돌린다).
    source_dir.chmod(0o555)
    try:
        probe = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                probe.execute("SELECT count(*) FROM ledger_rows").fetchone()
        finally:
            probe.close()

        writer_at_lock = threading.Event()
        _observe_runner_lock_acquisition(monkeypatch, writer_at_lock)

        snapshot_locked = threading.Event()
        release_snapshot = threading.Event()
        snapshot_errors: list[BaseException] = []
        snapshot_thread = _paused_snapshot_thread(
            source, destination, snapshot_locked, release_snapshot, snapshot_errors
        )

        writer_in_domestic = threading.Event()
        destination_seen_at_domestic: list[bool] = []

        def racing_domestic(conn, *, symbols, as_of_kst):
            destination_seen_at_domestic.append(destination.exists())
            writer_in_domestic.set()
            return fake_domestic(conn, symbols=symbols, as_of_kst=as_of_kst)

        writer_results: list[dict] = []
        writer_errors: list[BaseException] = []
        writer_deps = RunnerDependencies(
            domestic=racing_domestic, domestic_price=fake_price, global_collect=fake_global, now=lambda: AS_OF
        )

        def writer() -> None:
            try:
                writer_results.append(run_once(source, dependencies=writer_deps))
            except BaseException as error:  # noqa: BLE001 — 스레드 밖에서 재검사
                writer_errors.append(error)

        writer_thread = threading.Thread(target=writer)
        snapshot_thread.start()
        try:
            assert snapshot_locked.wait(timeout=30)
            # 스냅샷이 실제 잠금을 쥔 상태 — acquire_snapshot_lock 이 빠지면 실패.
            with pytest.raises(LedgerLockError):
                acquire_writer_lock(source, timeout=0.2)
            writer_thread.start()
            # writer 가 실제 잠금 획득 시도에 도달했음을 먼저 결정적으로 증명한다 —
            # run_once 의 acquire_writer_lock 이 빠지면 이 이벤트가 set 되지 않아 실패.
            assert writer_at_lock.wait(timeout=30)
            # 잠금 시도에 도달한 writer 는 immutable 관찰~백업~발행 전 구간 동안
            # 배타 flock 에 막혀 있다 ('미스케줄' 오탐 불가).
            assert not writer_in_domestic.wait(timeout=1.0)
            assert not destination.exists()
        finally:
            release_snapshot.set()
            snapshot_thread.join(timeout=60)
            if writer_thread.ident is not None:
                writer_thread.join(timeout=60)
    finally:
        source_dir.chmod(0o755)

    assert snapshot_errors == []
    assert writer_errors == []
    assert [summary["status"] for summary in writer_results] == ["ok"]
    # writer 는 스냅샷 발행 이후에만 stage 에 진입했다.
    assert destination_seen_at_domestic == [True]
    # 스냅샷은 pre-writer 스냅샷이다: 합성 행만 있고 writer 가 만든 KSF 스키마가 없다.
    assert _snapshot_rows(destination) == {"checkpointed-row", "uncheckpointed-row"}
    conn = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'ksf_%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()
    # 원본에는 잠금 해제 후 writer 커밋이 실제로 반영되었다.
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        assert src.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='ksf_runs'"
        ).fetchone()[0] == 1
        assert src.execute("SELECT COUNT(*) FROM ksf_runs").fetchone()[0] > 0
        assert {row[0] for row in src.execute("SELECT label FROM ledger_rows")} == {
            "checkpointed-row", "uncheckpointed-row",
        }
    finally:
        src.close()


def test_cli_exit_codes(tree, capsys: pytest.CaptureFixture):
    source, source_dir, run_dir = tree
    destination = run_dir / "ksf-ledger.sqlite3"
    _create_wal_source(source, close=True)
    with _read_only_tree(source_dir):
        assert helper.main([str(source), str(destination)]) == 0
    assert _snapshot_rows(destination) == {"checkpointed-row", "uncheckpointed-row"}
    assert helper.main([str(source), str(source)]) == 1
    error_output = capsys.readouterr().err
    assert "prepare_ksf_dashboard_snapshot" in error_output
