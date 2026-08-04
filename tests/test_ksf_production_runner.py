from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from inference.k_semiconductor_domestic_price_collector import DomesticPriceRecord
from inference.k_semiconductor_investor_flow_collector import InvestorFlowRecord


ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
AS_OF = datetime(2026, 7, 31, 16, 10, tzinfo=KST)


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

    assert connected_paths == [expected_db.resolve()]
    assert list(tmp_path.rglob("*")) == [parent, expected_db]
    assert parent.stat().st_mode & 0o777 == 0o700
    assert expected_db.stat().st_mode & 0o777 == 0o600


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
    assert "flock -n" in wrapper
    assert 'readonly LEDGER_DB="/srv/kronostock/data/ksf_ledger.sqlite3"' in wrapper
    assert '--db "$LEDGER_DB"' in wrapper
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
