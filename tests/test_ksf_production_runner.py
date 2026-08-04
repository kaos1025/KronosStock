from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from inference.k_semiconductor_domestic_price_collector import DomesticPriceRecord
from inference.k_semiconductor_investor_flow_collector import InvestorFlowRecord


ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
AS_OF = datetime(2026, 7, 31, 16, 10, tzinfo=KST)
SNAPSHOT_SCRIPT = ROOT / "scripts" / "deploy" / "prepare_ksf_dashboard_snapshot.py"


def _load_snapshot_helper():
    spec = importlib.util.spec_from_file_location("ksf_snapshot_helper_for_runner_tests", SNAPSHOT_SCRIPT)
    assert spec and spec.loader, f"snapshot helper missing: {SNAPSHOT_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_domestic(conn, *, symbols, as_of_kst):
    from inference.k_semiconductor_investor_flow_collector import InvestorFlowRepository

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    records = [
        InvestorFlowRecord(
            symbol=symbol,
            trade_date=AS_OF.date(),
            investor_category=category,
            source_taxonomy=taxonomy,
            net_buy_quantity=quantity,
            net_buy_amount_million_krw=None,
            source_status="INTRADAY_ESTIMATE",
            source_name=source,
        )
        for symbol in symbols
        for category, taxonomy, quantity, source in (
            ("foreigner", "KIS_FOREIGNER", 100, "KIS_INQUIRE_INVESTOR"),
            ("institution_total", "KIS_INSTITUTION_TOTAL", 50, "KIS_INQUIRE_INVESTOR"),
            ("kis_fund", "KIS_FUND", 10, "KIS_INVESTOR_TRADE_BY_STOCK_DAILY"),
        )
    ]
    return InvestorFlowRepository(conn).store_records(records, as_of_kst=as_of_kst)


def fake_price(conn, *, symbols, as_of_kst, available_data_cutoff):
    from inference.k_semiconductor_domestic_price_collector import DomesticPriceRepository

    assert available_data_cutoff == as_of_kst
    records = [
        DomesticPriceRecord(
            symbol=symbol,
            trade_date=AS_OF.date(),
            close=80000.0 if symbol == "005930" else 210000.0,
            volume=12345678 if symbol == "005930" else 9876543,
            source_as_of="2026-07-31T15:30:00+09:00",
            source_status="INTRADAY_ESTIMATE",
            one_day_return_bps=100.0,
        )
        for symbol in symbols
    ]
    return DomesticPriceRepository(conn).store_records(
        records, as_of_kst=as_of_kst, available_data_cutoff=available_data_cutoff
    )


class FakeGlobalResult:
    snapshots_inserted = 1
    features_inserted = 1
    source_metadata_inserted = 1
    statuses = {"optional_peer": "MISSING"}
    fallback_used = {}


def fake_global(conn, *, symbols, trading_date, as_of_kst, available_data_cutoff):
    assert tuple(symbols) == ("005930", "000660")
    return {symbol: FakeGlobalResult() for symbol in symbols}


def run_fake(tmp_path, **overrides):
    from ksf.production_runner import RunnerDependencies, run_once

    deps = RunnerDependencies(
        domestic=fake_domestic,
        domestic_price=fake_price,
        global_collect=fake_global,
        now=lambda: AS_OF,
        **overrides,
    )
    return run_once(tmp_path / "ledger" / "ksf.sqlite3", dependencies=deps)


def test_migration_and_rerun_are_idempotent_with_exact_symbol_isolation(tmp_path):
    first = run_fake(tmp_path)
    second = run_fake(tmp_path)
    db = tmp_path / "ledger" / "ksf.sqlite3"

    assert first["symbols"] == second["symbols"]
    assert [row["symbol"] for row in first["symbols"]] == ["005930", "000660"]
    assert second["stages"]["domestic"]["inserted_features"] == 0
    assert first["stages"]["migration"] == {"status": "ok", "schema_versions": [1, 2]}
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT version, COUNT(*) FROM ksf_schema_versions GROUP BY version ORDER BY version"
        ).fetchall() == [(1, 1), (2, 1)]
        assert set(row[0] for row in conn.execute("SELECT DISTINCT symbol FROM ksf_runs")) == {"005930", "000660"}
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_optional_global_failure_is_nonfatal_and_boundaries_are_not_activated(tmp_path):
    summary = run_fake(tmp_path)

    assert summary["status"] == "ok"
    assert summary["stages"]["global"]["missing_sources"] == 2
    assert summary["stages"]["ai_explanation"] == {"status": "not_activated"}
    assert summary["stages"]["performance_settlement"] == {"status": "not_activated"}
    assert all("missing_required" in item and "missing_optional" in item for item in summary["symbols"])


def test_default_domestic_dependency_uses_latest_xkrx_session_strictness(monkeypatch):
    import ksf.production_runner as production_runner

    captured: dict[str, object] = {}

    class DummyCollector:
        def __init__(self, *, client, now, stale_after_days):
            captured["client_type"] = type(client).__name__
            captured["now"] = now()
            captured["stale_after_days"] = stale_after_days

        def collect_symbols(self, symbols):
            captured["symbols"] = tuple(symbols)
            return []

    class DummyRepository:
        def __init__(self, conn):
            captured["foreign_keys"] = conn.execute("PRAGMA foreign_keys").fetchone()[0]

        def store_records(self, records, *, as_of_kst):
            captured["records"] = list(records)
            captured["as_of_kst"] = as_of_kst
            return type("Result", (), {"inserted_runs": 0, "inserted_snapshots": 0, "inserted_features": 0})()

    monkeypatch.setattr(production_runner, "InvestorFlowCollector", DummyCollector)
    monkeypatch.setattr(production_runner, "InvestorFlowRepository", DummyRepository)
    monkeypatch.setattr(production_runner, "KisInvestorFlowClient", lambda: object())
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")

    production_runner._default_domestic(conn, symbols=("005930",), as_of_kst="2026-08-03T16:10:00+09:00")

    assert captured == {
        "client_type": "object",
        "now": datetime(2026, 8, 3, 16, 10, tzinfo=KST),
        "stale_after_days": 0,
        "symbols": ("005930",),
        "foreign_keys": 1,
        "records": [],
        "as_of_kst": "2026-08-03T16:10:00+09:00",
    }


def test_runner_trading_date_is_latest_completed_xkrx_session_not_calendar_date(tmp_path):
    from ksf.production_runner import RunnerDependencies, run_once

    captured: dict[str, str] = {}

    def capturing_global(conn, *, symbols, trading_date, as_of_kst, available_data_cutoff):
        captured["trading_date"] = trading_date
        return {symbol: FakeGlobalResult() for symbol in symbols}

    # Sunday 2026-08-02: raw KST date is not a session; latest completed session is Friday 07-31.
    weekend_now = datetime(2026, 8, 2, 10, 0, tzinfo=KST)
    deps = RunnerDependencies(
        domestic=fake_domestic,
        domestic_price=fake_price,
        global_collect=capturing_global,
        now=lambda: weekend_now,
    )

    summary = run_once(tmp_path / "ledger.sqlite3", dependencies=deps)

    assert summary["status"] == "ok"
    assert captured["trading_date"] == "2026-07-31"


def test_runner_fails_closed_when_session_resolution_unavailable(tmp_path, capsys):
    from ksf.production_runner import RunnerDependencies, main

    def broken_session(now):
        raise RuntimeError("xkrx unavailable")

    deps = RunnerDependencies(
        domestic=fake_domestic,
        domestic_price=fake_price,
        global_collect=fake_global,
        now=lambda: AS_OF,
        trading_session=broken_session,
    )
    rc = main(["--db", str(tmp_path / "ledger.sqlite3")], dependencies=deps)

    assert rc != 0
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "failed_stage": "trading_session"}


def test_domestic_failure_is_hard_and_does_not_leak_secret(tmp_path, capsys):
    from ksf.production_runner import RunnerDependencies, main

    secret = "TOP-SECRET-TOKEN"

    def fail_domestic(*args, **kwargs):
        raise RuntimeError(f"domestic failed with {secret}")

    deps = RunnerDependencies(domestic=fail_domestic, domestic_price=fake_price, global_collect=fake_global, now=lambda: AS_OF)
    rc = main(["--db", str(tmp_path / "ledger.sqlite3")], dependencies=deps)
    output = capsys.readouterr().out

    assert rc != 0
    assert secret not in output
    payload = json.loads(output)
    assert payload == {"status": "failed", "failed_stage": "domestic"}


def test_domestic_price_failure_is_hard_and_does_not_leak_secret(tmp_path, capsys):
    from ksf.production_runner import RunnerDependencies, main

    secret = "TOP-SECRET-PRICE"

    def fail_price(*args, **kwargs):
        raise RuntimeError(f"price failed with {secret}")

    deps = RunnerDependencies(domestic=fake_domestic, domestic_price=fail_price, global_collect=fake_global, now=lambda: AS_OF)
    rc = main(["--db", str(tmp_path / "ledger.sqlite3")], dependencies=deps)
    output = capsys.readouterr().out

    assert rc != 0
    assert secret not in output
    assert json.loads(output) == {"status": "failed", "failed_stage": "domestic_price"}


def test_feature_gate_fails_when_supported_symbol_has_missing_or_stale_required(tmp_path, capsys):
    from ksf.production_runner import RunnerDependencies, main

    def no_price(conn, *, symbols, as_of_kst, available_data_cutoff):  # noqa: ARG001
        class Result:
            inserted_runs = 0
            inserted_snapshots = 0
            inserted_features = 0

        return Result()

    deps = RunnerDependencies(domestic=fake_domestic, domestic_price=no_price, global_collect=fake_global, now=lambda: AS_OF)
    rc = main(["--db", str(tmp_path / "ledger.sqlite3")], dependencies=deps)

    assert rc != 0
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "failed_stage": "feature_gate"}


def test_runner_fails_closed_when_ordered_upgrade_migration_is_missing(tmp_path, capsys):
    from ksf.production_runner import MIGRATIONS, RunnerDependencies, main

    # Applying only 001 must fail the required-versions check (1 and 2 exactly once each).
    deps = RunnerDependencies(
        domestic=fake_domestic,
        domestic_price=fake_price,
        global_collect=fake_global,
        now=lambda: AS_OF,
        migration_paths=(MIGRATIONS[0],),
    )
    rc = main(["--db", str(tmp_path / "ledger.sqlite3")], dependencies=deps)

    assert rc != 0
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "failed_stage": "migration"}


def test_cli_fails_closed_without_explicit_db(monkeypatch, capsys):
    from ksf.production_runner import main

    monkeypatch.delenv("KSF_LEDGER_DB_PATH", raising=False)
    assert main([]) != 0
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "failed_stage": "configuration"}


def test_parent_directory_is_private_and_run_uses_only_tmp_ledger_path(tmp_path, monkeypatch):
    import ksf.production_runner as production_runner

    parent = tmp_path / "ledger"
    expected_db = parent / "ksf.sqlite3"
    connected_paths = []
    real_connect = production_runner.sqlite3.connect

    def recording_connect(database, *args, **kwargs):
        connected_paths.append(Path(database).resolve())
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(production_runner.sqlite3, "connect", recording_connect)
    run_fake(tmp_path)

    expected_lock = Path(f"{expected_db}.lock")
    assert connected_paths == [expected_db.resolve()]
    assert sorted(tmp_path.rglob("*")) == sorted([parent, expected_db, expected_lock])
    assert parent.stat().st_mode & 0o777 == 0o700
    assert expected_db.stat().st_mode & 0o777 == 0o600
    assert stat.S_ISREG(expected_lock.stat().st_mode)
    assert expected_lock.stat().st_mode & 0o777 == 0o600


def test_runner_source_has_no_order_or_broker_imports():
    source = (ROOT / "ksf" / "production_runner.py").read_text(encoding="utf-8")
    assert "order" not in source.lower()
    assert "broker" not in source.lower()


def test_wrapper_and_systemd_contracts_are_lock_friendly_and_post_close():
    wrapper = (ROOT / "scripts" / "deploy" / "kronostock-ksf-once.sh").read_text(encoding="utf-8")
    service = (ROOT / "deploy" / "systemd" / "kronostock-ksf.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "systemd" / "kronostock-ksf.timer").read_text(encoding="utf-8")

    assert "set -euo pipefail" in wrapper
    assert "umask 077" in wrapper
    # 러너가 <ledger>.lock 을 내부에서 직접 획득하므로 외부 셸 잠금 래핑은
    # 제거되었다 (이중 잠금 유지 시 자기교착 가능).
    assert "flock" not in wrapper
    assert "LOCK_FILE" not in wrapper
    assert "/run/lock" not in wrapper
    assert 'readonly LEDGER_DB="/srv/kronostock/data/ksf_ledger.sqlite3"' in wrapper
    assert '--db "$LEDGER_DB"' in wrapper
    # 배포 경로는 여전히 러너 모듈 직접 exec 이다 (외부 잠금 래퍼 프로세스 없음).
    assert 'exec .venv/bin/python -m ksf.production_runner --db "$LEDGER_DB"' in wrapper
    assert "User=deploy" in service and "Group=deploy" in service
    assert "WorkingDirectory=/srv/agent-workspaces/KronosStock" in service
    assert "TimeoutStartSec=" in service
    assert "Mon..Fri 16:10:00 Asia/Seoul" in timer
    assert "Persistent=true" not in timer


def test_wrapper_sources_private_env_explicitly_without_printing_values():
    wrapper = (ROOT / "scripts" / "deploy" / "kronostock-ksf-once.sh").read_text(encoding="utf-8")

    assert 'readonly ENV_FILE="$APP_DIR/.env"' in wrapper
    assert '[ -f "$ENV_FILE" ]' in wrapper
    assert "Missing required private env file" in wrapper
    assert "set -a" in wrapper
    assert 'source "$ENV_FILE"' in wrapper
    assert "set +a" in wrapper
    assert wrapper.index('source "$ENV_FILE"') < wrapper.index(".venv/bin/python -m ksf.production_runner")
    assert "ALPHAVANTAGE_API_KEY" not in wrapper
    assert "BOK_ECOS_KEY" not in wrapper
    assert "printenv" not in wrapper
    assert "env |" not in wrapper


def test_runbook_preserves_old_timer_until_successful_zero_gap_cutover():
    runbook = (ROOT / "docs" / "k-semiconductor-flow-desk-production-runbook.md").read_text(encoding="utf-8")

    backup = runbook.index(".backup '$BACKUP/ksf_ledger.sqlite3'")
    install_wrapper = runbook.index("sudo install -o deploy -g deploy -m 700 scripts/deploy/kronostock-ksf-once.sh")
    daemon_reload = runbook.index("sudo systemctl daemon-reload")
    smoke = runbook.index("sudo -u deploy /srv/kronostock/kronostock-ksf-once.sh")
    integrity = runbook.index("PRAGMA integrity_check; PRAGMA foreign_key_check;")
    enable_ksf = runbook.index("sudo systemctl enable --now kronostock-ksf.timer")
    disable_old = runbook.index("sudo systemctl disable --now kronostock-dry-run.timer")

    assert backup < install_wrapper < daemon_reload < smoke < integrity < enable_ksf < disable_old
    assert "old dry-run timer remains active" in runbook
    assert "Any smoke failure stops the cutover" in runbook
    assert "Never enable both timers for the same scheduled window" in runbook


def test_readme_requires_deploy_time_lock_creation_and_fail_closed_startup():
    """README 는 배포 시점 잠금 파일 생성(전체 경로, 0600, deploy:deploy)을 명시하고,
    잠금이 없거나 unsafe 하면 대시보드 시작이 실패한다는 것을 기술해야 한다."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "/srv/kronostock/data/ksf_ledger.sqlite3.lock" in readme
    # 배포 시 1회 생성 명령 — 경로 생략(…) 없이 전체 경로 그대로.
    assert (
        "install -o deploy -g deploy -m 0600 /dev/null /srv/kronostock/data/ksf_ledger.sqlite3.lock"
        in readme
    )
    # 없거나 unsafe(심볼릭 링크·비정규 파일·타 소유자·group/world 권한)면 시작 실패.
    assert "없거나 unsafe" in readme
    assert "ExecStartPre" in readme


# --- Codex closure B: 원장 사이드카 잠금 — writer 전체 구간 fencing ---


def test_run_once_holds_sibling_lock_for_entire_run_and_serializes_writers(tmp_path):
    from ksf.production_runner import RunnerDependencies, run_once

    db = tmp_path / "ledger" / "ksf.sqlite3"
    first_in_domestic = threading.Event()
    release_first = threading.Event()
    order: list[str] = []

    def pausing_domestic(conn, *, symbols, as_of_kst):
        first_in_domestic.set()
        assert release_first.wait(timeout=30)
        return fake_domestic(conn, symbols=symbols, as_of_kst=as_of_kst)

    def recording_domestic(conn, *, symbols, as_of_kst):
        order.append("second-writer-domestic")
        return fake_domestic(conn, symbols=symbols, as_of_kst=as_of_kst)

    results: list[dict] = []

    def writer(domestic):
        deps = RunnerDependencies(
            domestic=domestic, domestic_price=fake_price, global_collect=fake_global, now=lambda: AS_OF
        )
        results.append(run_once(db, dependencies=deps))

    first = threading.Thread(target=writer, args=(pausing_domestic,))
    first.start()
    assert first_in_domestic.wait(timeout=30)
    second = threading.Thread(target=writer, args=(recording_domestic,))
    second.start()
    time.sleep(0.7)
    # 첫 writer 가 stage 중간에 멈춰 있는 동안(=아직 잠금 보유) 두 번째 writer 는
    # DB 를 열지도, stage 를 진행하지도 못한다.
    assert second.is_alive()
    assert order == []
    release_first.set()
    first.join(timeout=60)
    second.join(timeout=60)
    assert order == ["second-writer-domestic"]
    assert [summary["status"] for summary in results] == ["ok", "ok"]


def test_cli_reports_lock_stage_on_contention_without_leaking_paths(tmp_path, capsys):
    from ksf.production_runner import RunnerDependencies, main

    db = tmp_path / "ledger" / "ksf.sqlite3"
    db.parent.mkdir(mode=0o700)
    lock_file = Path(f"{db}.lock")
    fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        deps = RunnerDependencies(
            domestic=fake_domestic,
            domestic_price=fake_price,
            global_collect=fake_global,
            now=lambda: AS_OF,
            lock_timeout=0.2,
        )
        rc = main(["--db", str(db)], dependencies=deps)
    finally:
        os.close(fd)
    output = capsys.readouterr().out
    assert rc != 0
    assert json.loads(output) == {"status": "failed", "failed_stage": "lock"}
    assert str(tmp_path) not in output
    # 잠금 획득 전에는 DB 를 열거나 만들지 않는다.
    assert not db.exists()


def test_cli_reports_lock_stage_for_unsafe_lock_and_does_not_open_db(tmp_path, capsys):
    from ksf.production_runner import RunnerDependencies, main

    db = tmp_path / "ledger" / "ksf.sqlite3"
    db.parent.mkdir(mode=0o700)
    lock_file = Path(f"{db}.lock")
    lock_file.write_bytes(b"")
    lock_file.chmod(0o644)
    deps = RunnerDependencies(
        domestic=fake_domestic, domestic_price=fake_price, global_collect=fake_global, now=lambda: AS_OF
    )
    rc = main(["--db", str(db)], dependencies=deps)
    output = capsys.readouterr().out
    assert rc != 0
    assert json.loads(output) == {"status": "failed", "failed_stage": "lock"}
    assert str(tmp_path) not in output
    assert not db.exists()
    # fail-closed: unsafe 잠금을 고치거나 덮어쓰지 않는다.
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o644


def test_second_writer_times_out_as_lock_stage_without_entering_collectors(tmp_path):
    """첫 writer 가 run 중간(잠금 보유)일 때, lock_timeout 이 짧은 두 번째 writer 는
    StageFailure("lock") 으로 실패하고 DB open·collector 의존성에 진입하지 않는다."""
    from ksf.production_runner import RunnerDependencies, StageFailure, run_once

    db = tmp_path / "ledger" / "ksf.sqlite3"
    first_in_domestic = threading.Event()
    release_first = threading.Event()

    def pausing_domestic(conn, *, symbols, as_of_kst):
        first_in_domestic.set()
        assert release_first.wait(timeout=30)
        return fake_domestic(conn, symbols=symbols, as_of_kst=as_of_kst)

    first_results: list[dict] = []
    first_deps = RunnerDependencies(
        domestic=pausing_domestic, domestic_price=fake_price, global_collect=fake_global, now=lambda: AS_OF
    )
    first = threading.Thread(target=lambda: first_results.append(run_once(db, dependencies=first_deps)))
    first.start()
    try:
        assert first_in_domestic.wait(timeout=30)
        entered: list[str] = []

        def forbidden(stage):
            def _record(*args, **kwargs):
                entered.append(stage)
                raise AssertionError(f"{stage} must not run while another writer holds the lock")

            return _record

        second_deps = RunnerDependencies(
            domestic=forbidden("domestic"),
            domestic_price=forbidden("domestic_price"),
            global_collect=forbidden("global"),
            now=forbidden("now"),
            lock_timeout=0.3,
        )
        with pytest.raises(StageFailure) as failure:
            run_once(db, dependencies=second_deps)
        assert failure.value.stage == "lock"
        # 잠금 단계에서 fail-closed: now()/collector 어느 것도 호출되지 않았다.
        assert entered == []
    finally:
        release_first.set()
        first.join(timeout=60)
    assert first_results and first_results[0]["status"] == "ok"


def test_writer_lock_is_held_through_connection_close_and_released_after(tmp_path, monkeypatch):
    """잠금은 sqlite 연결 close 시점까지 유지되어야 하고(스냅샷과의 상호 배제 구간),
    run_once 반환 후에만 해제된다."""
    from ksf import production_runner

    db = tmp_path / "ledger" / "ksf.sqlite3"
    lock_file = Path(f"{db}.lock")

    def lock_is_held() -> bool:
        fd = os.open(lock_file, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    held_at_close: list[bool] = []

    class ProbingConnection(sqlite3.Connection):
        def close(self):
            held_at_close.append(lock_is_held())
            super().close()

    real_connect = sqlite3.connect

    def probing_connect(database, *args, **kwargs):
        kwargs.setdefault("factory", ProbingConnection)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(production_runner.sqlite3, "connect", probing_connect)
    summary = run_fake(tmp_path)
    assert summary["status"] == "ok"
    # 연결 close 가 실행되는 순간에도 sibling 잠금은 여전히 배타 보유 상태였다.
    assert held_at_close == [True]
    # run_once 반환 후에는 해제되어 다른 writer 가 즉시 획득할 수 있다.
    assert not lock_is_held()


def test_writer_blocks_while_snapshot_holds_lock_until_publication(tmp_path):
    """레이스 B: 스냅샷이 잠금 획득 직후 (내부 테스트 seam 으로) 정지해 있는 동안,
    실제 프로덕션 잠금 경로를 쓰는 writer(run_once)는 열지도 커밋하지도 못한다.
    스냅샷 발행 + 잠금 해제 이후에만 writer 커밋이 일어난다."""
    helper = _load_snapshot_helper()
    from ksf.production_runner import RunnerDependencies, run_once

    db = tmp_path / "ledger" / "ksf.sqlite3"
    run_fake(tmp_path)  # 원장 + 사이드카 잠금 생성 (WAL, close 후 사이드카 없음)
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    destination = run_dir / "ksf-ledger.sqlite3"
    source_hash_before = hashlib.sha256(db.read_bytes()).hexdigest()

    paused = threading.Event()
    resume = threading.Event()
    snapshot_errors: list[BaseException] = []

    def pause_after_lock():
        paused.set()
        assert resume.wait(timeout=30)

    def run_snapshot():
        try:
            helper.create_snapshot(db, destination, _after_lock_acquired=pause_after_lock)
        except BaseException as error:  # noqa: BLE001 — 스레드 경계에서 전부 수집
            snapshot_errors.append(error)

    snapshot_thread = threading.Thread(target=run_snapshot)
    snapshot_thread.start()
    writer_thread: threading.Thread | None = None
    try:
        deadline = time.monotonic() + 30
        while not paused.is_set() and snapshot_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not snapshot_errors, snapshot_errors
        assert paused.is_set()

        writer_saw_snapshot: list[bool] = []

        def observing_now():
            # run_once 는 잠금 + DB open 이후에만 now() 를 호출한다. 잠금이 발행
            # 시점까지 유지되므로 이때는 스냅샷이 이미 존재해야 한다.
            writer_saw_snapshot.append(destination.exists())
            return AS_OF

        deps = RunnerDependencies(
            domestic=fake_domestic, domestic_price=fake_price, global_collect=fake_global, now=observing_now
        )
        writer_results: list[dict] = []
        writer_thread = threading.Thread(target=lambda: writer_results.append(run_once(db, dependencies=deps)))
        writer_thread.start()
        time.sleep(1.0)
        # 스냅샷이 잠금을 쥔 동안: writer 진행 없음, 원본 불변, 발행물 없음.
        assert writer_thread.is_alive()
        assert writer_saw_snapshot == []
        assert hashlib.sha256(db.read_bytes()).hexdigest() == source_hash_before
        assert not destination.exists()
    finally:
        resume.set()
    snapshot_thread.join(timeout=30)
    assert writer_thread is not None
    writer_thread.join(timeout=90)
    assert not snapshot_thread.is_alive() and not writer_thread.is_alive()
    assert snapshot_errors == []
    assert writer_results and writer_results[0]["status"] == "ok"
    assert writer_saw_snapshot == [True]
    assert destination.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="requires non-root permission semantics")
def test_codex_race_immutable_fallback_is_fenced_by_writer_exclusion_lock(tmp_path):
    """레이스 C (Codex B 재현): 사이드카가 둘 다 없는 상태에서 스냅샷이 잠금을 획득한
    뒤 immutable 폴백 이전에 정지. 그 사이 시작한 writer 는 immutable 백업이 끝날
    때까지 차단되고, 스냅샷은 writer 이전 시점의 일관된 뷰다 (잠금 획득 이전 커밋은
    전부 포함, writer 커밋은 미포함)."""
    helper = _load_snapshot_helper()
    from ksf.production_runner import RunnerDependencies, run_once

    source_dir = tmp_path / "data"
    source_dir.mkdir()
    source = source_dir / "ksf_ledger.sqlite3"
    lock_file = Path(f"{source}.lock")
    fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    destination = run_dir / "ksf-ledger.sqlite3"

    toy = sqlite3.connect(source)
    toy.execute("PRAGMA journal_mode=WAL")
    toy.execute("PRAGMA wal_autocheckpoint=0")
    toy.execute("CREATE TABLE ledger_rows (label TEXT NOT NULL)")
    toy.execute("INSERT INTO ledger_rows VALUES ('checkpointed-row')")
    toy.commit()
    toy.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    toy.execute("INSERT INTO ledger_rows VALUES ('uncheckpointed-row')")
    toy.commit()
    toy.close()  # 마지막 연결 종료 → 최종 체크포인트 + 사이드카 삭제
    assert not Path(f"{source}-wal").exists() and not Path(f"{source}-shm").exists()

    # 읽기 전용 데이터 트리 (잠금 파일만 0600 유지 — 프로덕션과 동일).
    source.chmod(0o444)
    source_dir.chmod(0o555)
    writer_thread = None
    snapshot_thread = None
    resume = threading.Event()
    try:
        # 폴백 전제 검증: 사이드카 없는 WAL DB 는 이 트리에서 mode=ro 로 읽을 수 없다.
        probe = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                probe.execute("SELECT count(*) FROM ledger_rows").fetchone()
        finally:
            probe.close()

        paused = threading.Event()
        snapshot_errors: list[BaseException] = []

        def pause_after_lock():
            paused.set()
            assert resume.wait(timeout=30)

        def run_snapshot():
            try:
                helper.create_snapshot(source, destination, _after_lock_acquired=pause_after_lock)
            except BaseException as error:  # noqa: BLE001
                snapshot_errors.append(error)

        snapshot_thread = threading.Thread(target=run_snapshot)
        snapshot_thread.start()
        deadline = time.monotonic() + 30
        while not paused.is_set() and snapshot_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not snapshot_errors, snapshot_errors
        assert paused.is_set()
        source_hash_at_lock = hashlib.sha256(source.read_bytes()).hexdigest()

        writer_saw_snapshot: list[bool] = []

        def observing_now():
            writer_saw_snapshot.append(destination.exists())
            return AS_OF

        deps = RunnerDependencies(
            domestic=fake_domestic, domestic_price=fake_price, global_collect=fake_global, now=observing_now
        )
        writer_results: list[dict] = []
        writer_thread = threading.Thread(target=lambda: writer_results.append(run_once(source, dependencies=deps)))
        writer_thread.start()
        time.sleep(1.0)
        # writer 는 immutable 백업 전 구간 내내 차단된다: 진행 없음, 원본 불변.
        assert writer_thread.is_alive()
        assert writer_saw_snapshot == []
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash_at_lock
        assert not destination.exists()

        # 프로덕션에서 writer 는 자기 소유의 쓰기 가능한 원장을 가진다 — 0444 는
        # 읽기 전용 트리 시뮬레이션용이므로, writer 가 아직 flock 에 차단된 동안
        # 파일 권한만 복구한다. 디렉터리는 0555 를 유지해 스냅샷의 정상 mode=ro
        # 열기가 계속 실패하고 immutable 폴백 경로가 그대로 검증되게 한다.
        source.chmod(0o644)

        resume.set()
        snapshot_thread.join(timeout=30)
        assert snapshot_errors == []
        writer_thread.join(timeout=90)
        assert not writer_thread.is_alive()
        assert writer_results and writer_results[0]["status"] == "ok"
        # writer 는 스냅샷 발행 + 잠금 해제 이후에만 실행되었다.
        assert writer_saw_snapshot == [True]
    finally:
        resume.set()
        if snapshot_thread is not None:
            snapshot_thread.join(timeout=30)
        if writer_thread is not None:
            writer_thread.join(timeout=90)
        source_dir.chmod(0o755)
        for path in source_dir.iterdir():
            path.chmod(0o600 if path.name.endswith(".lock") else 0o644)

    # 스냅샷 = writer 이전 point-in-time 뷰: 잠금 획득 이전 커밋은 모두 포함하고
    # (silent 커밋 유실 없음), writer 의 마이그레이션/커밋은 포함하지 않는다.
    snap = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        assert {row[0] for row in snap.execute("SELECT label FROM ledger_rows")} == {
            "checkpointed-row", "uncheckpointed-row",
        }
        snap_tables = {row[0] for row in snap.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "ksf_runs" not in snap_tables
    finally:
        snap.close()
    with sqlite3.connect(source) as conn:
        source_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ksf_runs" in source_tables  # writer 커밋은 스냅샷 이후 원본에만 존재


def test_module_cli_never_mentions_environment_values(tmp_path):
    env = {"PATH": "", "PYTHONPATH": str(ROOT), "KSF_LEDGER_DB_PATH": ""}
    result = subprocess.run(
        [sys.executable, "-m", "ksf.production_runner"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stderr == ""
    assert json.loads(result.stdout)["failed_stage"] == "configuration"
