"""Task 6 autonomous paper-cycle orchestration contract (offline only)."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import threading
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from ksf.feature_engine import FeatureEngine
from paper_trading.ledger import LedgerConflictError, LedgerError, PaperLedger
from strategy.paper_broker import ExecutionPolicy, MarketObservation
from strategy.paper_cycle import (
    CycleConfig, CycleInput, CycleLineage, CycleResult, PaperCycleDisabled,
    main, run_cycle, run_cycle_from_env,
)
from strategy.paper_risk import RiskPolicy


PRODUCER_WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "produce-paper-response-bundle-once.sh"


NOW = "2026-08-08T10:00:00+09:00"
CUTOFF = "2026-08-08T09:59:00+09:00"


def _snapshot(symbol="005930", *, omit=(), cutoff=CUTOFF, as_of=NOW,
              source_as_of="2026-08-08T09:58:30+09:00",
              ingested_at="2026-08-08T09:58:31+09:00"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE ksf_normalized_features (
        feature_id TEXT, symbol TEXT, feature_group TEXT, feature_name TEXT,
        value_num REAL, unit TEXT, source_as_of TEXT, feature_status TEXT,
        missing_reason TEXT, ingested_at_kst TEXT, available_data_cutoff TEXT)""")
    rows = {
        "close": (70_000.0, "KRW", "domestic_price"),
        "volume": (1000.0, "shares", "domestic_price"),
        "foreigner_net_buy_quantity": (100.0, "shares", "domestic_investor_flow"),
        "institution_total_net_buy_quantity": (100.0, "shares", "domestic_investor_flow"),
    }
    for name, (value, unit, group) in rows.items():
        if name not in omit:
            conn.execute("INSERT INTO ksf_normalized_features VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                "f-" + name, symbol, group, name, value, unit,
                source_as_of, "READY", None,
                ingested_at, ingested_at))
    result = FeatureEngine(conn).build_symbol(symbol, available_data_cutoff=cutoff, as_of_kst=as_of)
    conn.close()
    return result


def _lineage(symbol="005930", snapshot=None):
    snapshot = snapshot or _snapshot(symbol)
    return CycleLineage(symbol=symbol, ksf_run_id="run-" + symbol,
        ksf_decision_id="ksf-" + symbol,
        feature_snapshot_sha256=hashlib.sha256(snapshot.stable_json().encode()).hexdigest(),
        available_data_cutoff=snapshot.available_data_cutoff, feature_snapshot=snapshot)


def _config(path, mode="shadow"):
    return CycleConfig(mode=mode, paper_db_path=path, account_id="acct",
        lock_path=Path(str(path) + ".lock"), source_name="fixture",
        risk_policy=RiskPolicy(2_000, 4_000, 4, 4, 500_000, 1_000, 50_000, 300),
        execution_policy=ExecutionPolicy())


def _input(*, transport=None, observations=(), lineages=None):
    def enter(request):
        return {
            "symbol": request["symbol"], "response_schema_version": request["response_schema_version"],
            "prompt_template_version": request["prompt_template_version"],
            "model_policy_version": request["model_policy_version"], "model_provider": request["model_provider"],
            "model_name": request["model_name"], "action": "ENTER", "target_exposure_pct": 20.0,
            "confidence_bps": 0, "thesis": "요청에 결속된 검증 근거에 따라 이 종목의 노출 확대를 제안합니다.",
            "evidence_ids": [request["evidence_catalog"][1]["id"]],
            "invalidation_conditions": [{"text": "인용한 근거의 검증 상태가 변하면 이 제안을 무효로 재검토합니다.",
                "evidence_ids": [request["evidence_catalog"][1]["id"]]}],
            "data_gaps": [], "abstain_reason": None,
        }
    return CycleInput(session_id="2026-08-08", cycle_at=NOW,
        lineages=tuple(lineages or (_lineage(),)), observations=tuple(observations),
        transport=transport or enter, model_provider="fixture", model_name="fixture-v1",
        response_bundle_sha256="c" * 64, source_artifact_sha256="d" * 64)


def _seed_account(path):
    ledger = PaperLedger(path)
    ledger.create_account(account_id="acct", initial_cash_krw=10_000_000,
        policy_version="v1", created_at="2026-08-07T08:00:00+09:00")
    ledger.append_account_event(account_event_id="enabled", account_id="acct",
        event_type="ENABLED", event_at="2026-08-08T08:01:00+09:00", reason_code="MANUAL")
    ledger.append_kill_switch_event(kill_switch_event_id="released", account_id="acct",
        event_type="RELEASED", reason_code="MANUAL", event_at="2026-08-08T08:02:00+09:00")
    ledger.close()


def _ksf_db(path, *, status="SCORING_DONE", cutoff=CUTOFF, as_of=NOW,
            source_as_of="2026-08-08T09:58:30+09:00",
            ingested_at="2026-08-08T09:58:31+09:00"):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE ksf_runs(run_id TEXT, symbol TEXT, trading_date TEXT, run_status TEXT,
      as_of_kst TEXT, available_data_cutoff TEXT, archived_at_kst TEXT);
    CREATE TABLE ksf_decisions(decision_id TEXT, run_id TEXT, symbol TEXT, horizon_days INTEGER,
      as_of_kst TEXT, available_data_cutoff TEXT, feature_snapshot_sha256 TEXT);
    CREATE TABLE ksf_normalized_features(feature_id TEXT,run_id TEXT,symbol TEXT,
      feature_group TEXT,feature_name TEXT,feature_version TEXT,feature_status TEXT,
      value_num REAL,value_text TEXT,value_json TEXT,unit TEXT,source_as_of TEXT,
      ingested_at_kst TEXT,available_data_cutoff TEXT,missing_reason TEXT);
    """)
    for symbol in sorted(("005930", "000660")):
        run = "run-" + symbol
        conn.execute("INSERT INTO ksf_runs VALUES(?,?,?,?,?,?,NULL)",
            (run, symbol, "2026-08-08", status, as_of, cutoff))
        rows = {
            "close": (70_000.0, "KRW", "domestic_price"), "volume": (1000.0, "shares", "domestic_price"),
            "foreigner_net_buy_quantity": (100.0, "shares", "domestic_investor_flow"),
            "institution_total_net_buy_quantity": (100.0, "shares", "domestic_investor_flow")}
        for name, (value, unit, group) in rows.items():
            conn.execute("INSERT INTO ksf_normalized_features VALUES(?,?,?,?,?,'v1','READY',?,NULL,NULL,?,?,?,?,NULL)",
                (symbol+name, run, symbol, group, name, value, unit,
                 source_as_of, ingested_at, cutoff))
        digest = hashlib.sha256(_snapshot(
            symbol, cutoff=cutoff, as_of=as_of, source_as_of=source_as_of,
            ingested_at=ingested_at,
        ).stable_json().encode()).hexdigest()
        conn.execute("INSERT INTO ksf_decisions VALUES(?,?,?,?,?,?,?)",
            ("ksf-"+symbol, run, symbol, 5, as_of, cutoff, digest))
    conn.commit(); conn.close()


def _runtime_env(tmp_path, paper, ksf, bundle):
    return {"PAPER_AGENT_ENABLED":"true", "PAPER_AGENT_MODE":"shadow",
        "PAPER_AGENT_DB_PATH":str(paper), "PAPER_AGENT_ACCOUNT_ID":"acct",
        "PAPER_AGENT_LOCK_PATH":str(tmp_path/"paper.lock"), "PAPER_AGENT_SOURCE":"ksf-sqlite",
        "PAPER_AGENT_KSF_DB_PATH":str(ksf), "PAPER_AGENT_HORIZON_DAYS":"5",
        "PAPER_AGENT_SESSION_ID":"2026-08-08", "PAPER_AGENT_CYCLE_AT":NOW,
        "PAPER_AGENT_RISK_POLICY_JSON":json.dumps(asdict(_config(paper).risk_policy)),
        "PAPER_AGENT_EXECUTION_POLICY_JSON":json.dumps(asdict(_config(paper).execution_policy)),
        "PAPER_AGENT_MODEL_PROVIDER":"offline", "PAPER_AGENT_MODEL_NAME":"fixture-v1",
        "PAPER_AGENT_RESPONSE_BUNDLE_PATH":str(bundle)}


def _write_bundle(path, ksf):
    from ksf.response_bundle import build_response_bundle
    lineage = {symbol: {"ksf_run_id": "run-" + symbol,
        "ksf_decision_id": "ksf-" + symbol,
        "feature_snapshot_sha256": hashlib.sha256(_snapshot(symbol).stable_json().encode()).hexdigest()}
        for symbol in ("005930", "000660")}
    source_sha = hashlib.sha256(ksf.read_bytes()).hexdigest() if ksf.exists() else "d" * 64
    value = build_response_bundle(session_id="2026-08-08", cycle_at=NOW,
        source_artifact_sha256=source_sha, lineage=lineage,
        responses={symbol: {} for symbol in lineage}, model_provider="offline", model_name="fixture-v1")
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _producer_env(tmp_path, ksf):
    return {"PAPER_AGENT_BUNDLE_PRODUCER_ENABLED":"true",
        "PAPER_AGENT_BUNDLE_DIR":str(tmp_path / "bundles"),
        "PAPER_AGENT_KSF_DB_PATH":str(ksf), "PAPER_AGENT_HORIZON_DAYS":"5",
        "PAPER_AGENT_SESSION_ID":"2026-08-08", "PAPER_AGENT_CYCLE_AT":NOW,
        "PAPER_AGENT_MODEL_PROVIDER":"offline-deterministic",
        "PAPER_AGENT_MODEL_NAME":"shadow-abstain-v1"}


def test_offline_producer_is_byte_stable_lineage_bound_and_no_overwrite(tmp_path):
    from ksf.offline_response_bundle_producer import produce_from_env
    ksf = tmp_path / "ksf.sqlite3"; _ksf_db(ksf)
    out_dir = tmp_path / "bundles"; out_dir.mkdir()
    env = _producer_env(tmp_path, ksf)
    first = produce_from_env(env)
    raw = first.read_bytes()
    value = json.loads(raw)
    assert first == out_dir / "2026-08-08.json"
    assert first.stat().st_mode & 0o777 == 0o600
    assert set(value["symbols"]) == {"005930", "000660"}
    assert value["schema_version"] == "ksf-response-bundle-v1"
    assert value["source_artifact_sha256"] == hashlib.sha256(ksf.read_bytes()).hexdigest()
    assert len(value["bundle_sha256"]) == 64 and value["bundle_sha256"] == value["bundle_sha256"].lower()
    assert {tuple(item["response"].items()) for item in value["symbols"].values()} == {
        (("offline_deterministic_abstain_reason", "OFFLINE_SHADOW_MODE"),)}
    with pytest.raises(LedgerError, match="already exists"):
        produce_from_env(env)
    assert first.read_bytes() == raw


@pytest.mark.parametrize("mutation", ["relative-source", "relative-output", "source-symlink",
    "output-symlink", "missing-horizon", "missing-decision", "duplicate-run"])
def test_offline_producer_rejects_unsafe_paths_and_weak_lineage(tmp_path, mutation):
    from ksf.offline_response_bundle_producer import produce_from_env
    ksf = tmp_path / "ksf.sqlite3"; _ksf_db(ksf)
    out_dir = tmp_path / "bundles"; out_dir.mkdir()
    env = _producer_env(tmp_path, ksf)
    if mutation == "relative-source": env["PAPER_AGENT_KSF_DB_PATH"] = "ksf.sqlite3"
    elif mutation == "relative-output": env["PAPER_AGENT_BUNDLE_DIR"] = "bundles"
    elif mutation == "source-symlink":
        link = tmp_path / "source-link"; link.symlink_to(ksf)
        env["PAPER_AGENT_KSF_DB_PATH"] = str(link)
    elif mutation == "output-symlink":
        real = tmp_path / "real-bundles"; real.mkdir()
        out_dir.rmdir(); out_dir.symlink_to(real, target_is_directory=True)
    elif mutation == "missing-horizon": env["PAPER_AGENT_HORIZON_DAYS"] = "20"
    elif mutation == "missing-decision":
        with sqlite3.connect(ksf) as conn: conn.execute("DELETE FROM ksf_decisions WHERE symbol='005930'")
    else:
        with sqlite3.connect(ksf) as conn:
            conn.execute("INSERT INTO ksf_runs VALUES(?,?,?,?,?,?,NULL)",
                ("duplicate", "005930", "2026-08-08", "SCORING_DONE", NOW, CUTOFF))
    with pytest.raises((LedgerError, PaperCycleDisabled)):
        produce_from_env(env)
    assert not (tmp_path / "paper.sqlite3").exists()


def test_produced_bundle_commits_two_shadow_abstentions_and_no_orders(tmp_path):
    from ksf.offline_response_bundle_producer import produce_from_env
    paper, ksf = tmp_path / "paper.sqlite3", tmp_path / "ksf.sqlite3"
    _seed_account(paper); _ksf_db(ksf); (tmp_path / "bundles").mkdir()
    bundle = produce_from_env(_producer_env(tmp_path, ksf))
    env = _runtime_env(tmp_path, paper, ksf, bundle)
    env.update(PAPER_AGENT_MODEL_PROVIDER="offline-deterministic",
        PAPER_AGENT_MODEL_NAME="shadow-abstain-v1")
    result = run_cycle_from_env(env)
    assert result.counts == {"decisions": 2, "proposals": 0, "reviews": 0, "orders": 0, "fills": 0}


def test_producer_wrapper_disabled_and_does_not_print_payload(tmp_path):
    disabled = subprocess.run([str(PRODUCER_WRAPPER)], env={"PATH": os.environ["PATH"]},
        text=True, capture_output=True, check=False)
    assert disabled.returncode == 0 and disabled.stdout == "response-bundle status=disabled\n"
    assert "symbols" not in disabled.stdout and "offline_deterministic_abstain_reason" not in disabled.stdout


def test_response_bundle_is_immutable_versioned_and_lineage_bound(tmp_path):
    from ksf.response_bundle import build_response_bundle, validate_response_bundle

    lineage = {
        symbol: {"ksf_run_id": "run-" + symbol, "ksf_decision_id": "ksf-" + symbol,
                 "feature_snapshot_sha256": ("a" if symbol == "005930" else "b") * 64}
        for symbol in ("005930", "000660")
    }
    responses = {symbol: {"action": "HOLD", "target_exposure_pct": 0,
        "confidence": 0.5, "thesis": "검증된 오프라인 응답", "evidence": [],
        "data_gaps": [], "abstain_reason": None} for symbol in lineage}
    bundle = build_response_bundle(session_id="2026-08-08", cycle_at=NOW,
        source_artifact_sha256="d" * 64, lineage=lineage, responses=responses,
        model_provider="offline", model_name="fixture-v1")
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    accepted = validate_response_bundle(path.read_bytes(), session_id="2026-08-08",
        cycle_at=NOW, source_artifact_sha256="d" * 64, lineage=lineage,
        model_provider="offline", model_name="fixture-v1")
    assert accepted == responses

    mutations = [
        lambda value: value.update(schema_version="wrong"),
        lambda value: value.update(session_id="2026-08-07"),
        lambda value: value.update(cycle_at="2026-08-08T09:59:59+09:00"),
        lambda value: value.update(source_artifact_sha256="e" * 64),
        lambda value: value["symbols"].pop("000660"),
        lambda value: value["symbols"]["005930"].update(ksf_run_id="wrong"),
        lambda value: value["symbols"]["005930"].update(ksf_decision_id="wrong"),
        lambda value: value["symbols"]["005930"].update(feature_snapshot_sha256="f" * 64),
        lambda value: value.update(bundle_sha256="0" * 64),
    ]
    for mutate in mutations:
        changed = json.loads(json.dumps(bundle))
        mutate(changed)
        with pytest.raises(LedgerError):
            validate_response_bundle(json.dumps(changed).encode(), session_id="2026-08-08",
                cycle_at=NOW, source_artifact_sha256="d" * 64, lineage=lineage,
                model_provider="offline", model_name="fixture-v1")


def test_env_runtime_loads_read_only_source_and_persists_real_shadow_cycle(tmp_path):
    paper, ksf, bundle = tmp_path/"paper.sqlite3", tmp_path/"ksf.sqlite3", tmp_path/"bundle.json"
    _seed_account(paper); _ksf_db(ksf)
    _write_bundle(bundle, ksf)
    result = run_cycle_from_env(_runtime_env(tmp_path, paper, ksf, bundle))
    assert result.committed and result.counts["decisions"] == 2
    with PaperLedger(paper) as ledger:
        assert ledger.conn.execute("SELECT count(*) FROM paper_cycle_commits").fetchone()[0] == 1


def test_source_accepts_exact_date_only_and_preserves_stored_value(tmp_path):
    from strategy.paper_source import load_ksf_sqlite

    ksf = tmp_path / "ksf.sqlite3"
    _ksf_db(ksf, source_as_of="2026-08-08")

    lineages, _ = load_ksf_sqlite(
        ksf.resolve(), session_id="2026-08-08", horizon_days=5,
        cycle_at=NOW, source_sha256=hashlib.sha256(ksf.read_bytes()).hexdigest(),
    )

    assert {
        feature.source_as_of
        for lineage in lineages
        for group in lineage.feature_snapshot.groups.values()
        for feature in group.features
    } == {"2026-08-08"}


def test_source_date_only_uses_cutoff_encoded_local_date_not_utc_date(tmp_path):
    from strategy.paper_source import load_ksf_sqlite

    ksf = tmp_path / "ksf.sqlite3"
    cutoff = "2026-08-08T23:30:00-10:00"  # 2026-08-09 in UTC, but local date is the 8th.
    cycle_at = "2026-08-08T23:31:00-10:00"
    _ksf_db(ksf, cutoff=cutoff, as_of=cycle_at, source_as_of="2026-08-09")

    with pytest.raises(LedgerError, match="future KSF feature row"):
        load_ksf_sqlite(
            ksf.resolve(), session_id="2026-08-08", horizon_days=5,
            cycle_at=cycle_at, source_sha256=hashlib.sha256(ksf.read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize(
    ("source_as_of", "message"),
    [
        ("2026-08-09", "future KSF feature row"),
        ("2026-08-08T09:58:30", "KSF feature timestamp is malformed"),
        ("2026-02-30", "KSF feature timestamp is malformed"),
        ("not-a-date", "KSF feature timestamp is malformed"),
    ],
)
def test_source_rejects_future_date_only_naive_datetime_and_malformed_values(
    tmp_path, source_as_of, message
):
    from strategy.paper_source import load_ksf_sqlite

    ksf = tmp_path / "ksf.sqlite3"
    _ksf_db(ksf, source_as_of=source_as_of)

    with pytest.raises(LedgerError, match=message):
        load_ksf_sqlite(
            ksf.resolve(), session_id="2026-08-08", horizon_days=5,
            cycle_at=NOW, source_sha256=hashlib.sha256(ksf.read_bytes()).hexdigest(),
        )


def test_invalid_source_status_precedes_lock_and_paper_ledger_mutation(tmp_path):
    paper, ksf, bundle = tmp_path/"absent.sqlite3", tmp_path/"ksf.sqlite3", tmp_path/"bundle.json"
    _ksf_db(ksf, status="PARTIAL_DATA"); _write_bundle(bundle, ksf)
    with pytest.raises(LedgerError, match="eligible exact-session"):
        run_cycle_from_env(_runtime_env(tmp_path, paper, ksf, bundle))
    assert not paper.exists() and not (tmp_path/"paper.lock").exists()


@pytest.mark.parametrize("corruption", ["cross-symbol", "future-optional"])
def test_source_rejects_hidden_cross_symbol_and_future_rows_before_paper_mutation(tmp_path, corruption):
    paper, ksf, bundle = tmp_path/"absent.sqlite3", tmp_path/"ksf.sqlite3", tmp_path/"bundle.json"
    _ksf_db(ksf); _write_bundle(bundle, ksf)
    conn = sqlite3.connect(ksf)
    if corruption == "cross-symbol":
        conn.execute("UPDATE ksf_normalized_features SET symbol='000660' WHERE run_id='run-005930' AND feature_name='volume'")
    else:
        conn.execute("INSERT INTO ksf_normalized_features VALUES(?,?,?,?,?,'v1','READY',?,NULL,NULL,?,?,?,?,NULL)",
            ("future", "run-005930", "005930", "event", "earnings_event_days", 1.0, "days",
             "2026-08-09T09:00:00+09:00", "2026-08-09T09:00:00+09:00", CUTOFF))
    conn.commit(); conn.close()
    with pytest.raises(LedgerError):
        run_cycle_from_env(_runtime_env(tmp_path, paper, ksf, bundle))
    assert not paper.exists() and not (tmp_path/"paper.lock").exists()


@pytest.mark.parametrize("env", [{}, {"PAPER_AGENT_ENABLED": "false"},
    {"PAPER_AGENT_ENABLED": "true"}, {"PAPER_AGENT_ENABLED": "true", "PAPER_AGENT_MODE": "bad"}])
def test_env_gate_rejects_before_ledger_source_or_lock(tmp_path, env):
    path = tmp_path / "must-not-exist.sqlite3"
    touched = []
    with pytest.raises(PaperCycleDisabled):
        run_cycle_from_env(env, ledger_factory=lambda p: touched.append("ledger"),
            source_loader=lambda: touched.append("source"), lock_factory=lambda p: touched.append("lock"))
    assert touched == [] and not path.exists()


def test_fills_env_requires_explicit_fills_gate_before_any_runtime_access(tmp_path):
    env = {"PAPER_AGENT_ENABLED": "true", "PAPER_AGENT_MODE": "fills"}
    touched = []
    with pytest.raises(PaperCycleDisabled, match="PAPER_AGENT_FILLS_ENABLED"):
        run_cycle_from_env(env, ledger_factory=lambda p: touched.append("ledger"),
            source_loader=lambda *a, **k: touched.append("source"),
            lock_factory=lambda p: touched.append("lock"))
    assert touched == []


def test_one_shot_wrapper_is_disabled_by_default_and_keeps_fill_gate_separate(tmp_path):
    wrapper = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "kronostock-paper-agent-once.sh"
    disabled = subprocess.run([str(wrapper)], env={"PATH": os.environ["PATH"]},
        text=True, capture_output=True, check=False)
    assert disabled.returncode == 0 and disabled.stdout == "paper-cycle status=disabled\n"
    fills = subprocess.run([str(wrapper)], env={"PATH": os.environ["PATH"],
        "PAPER_AGENT_ENABLED": "true", "PAPER_AGENT_MODE": "fills"},
        text=True, capture_output=True, check=False)
    assert fills.returncode != 0 and fills.stderr == "paper-cycle status=failed\n"
    source = wrapper.read_text()
    assert 'bundle_path="${PAPER_AGENT_BUNDLE_DIR%/}/${session_id}.json"' in source
    assert 'export PAPER_AGENT_SESSION_ID="${session_id}"' in source
    assert 'export PAPER_AGENT_CYCLE_AT="${cycle_at}"' in source
    assert '[[ -f "${bundle_path}" && ! -L "${bundle_path}" ]]' in source
    assert source.index('[[ -f "${bundle_path}"') < source.index("exec /srv/agent-workspaces")


@pytest.mark.parametrize("missing", ["bundle", "source"])
def test_enabled_runtime_missing_artifact_is_failed_not_disabled(tmp_path, monkeypatch, capsys, missing):
    ksf, bundle = tmp_path/"ksf.sqlite3", tmp_path/"bundle.json"
    if missing != "source": _ksf_db(ksf)
    if missing != "bundle": _write_bundle(bundle, ksf)
    env = _runtime_env(tmp_path, tmp_path/"paper.sqlite3", ksf, bundle)
    monkeypatch.setattr("strategy.paper_cycle.os.environ", env)
    assert main(["--once"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "paper-cycle status=failed\n"
    assert str(tmp_path) not in captured.err


def test_source_artifact_hash_is_exact_file_bytes_and_passed_to_loader(tmp_path):
    paper, ksf, bundle = tmp_path/"paper.sqlite3", tmp_path/"ksf.sqlite3", tmp_path/"bundle.json"
    _seed_account(paper); _ksf_db(ksf)
    _write_bundle(bundle, ksf)
    seen = []
    from strategy.paper_source import load_ksf_sqlite
    def loader(path, **kwargs):
        seen.append(kwargs["source_sha256"])
        return load_ksf_sqlite(path, **kwargs)
    result = run_cycle_from_env(_runtime_env(tmp_path, paper, ksf, bundle), source_loader=loader)
    assert result.committed
    assert seen == [hashlib.sha256(ksf.read_bytes()).hexdigest()]


def test_shadow_persists_decision_and_reject_audit_but_no_economic_events(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    config = replace(_config(path), risk_policy=RiskPolicy(
        2_000, 4_000, 4, 4, 500_000, 1_000, 20_000_000, 300))
    result = run_cycle(config, _input())
    assert result.committed and result.mode == "shadow" and result.counts["decisions"] == 1
    with PaperLedger(path) as ledger:
        assert ledger.conn.execute("SELECT count(*) FROM paper_agent_decisions").fetchone()[0] == 1
        assert ledger.conn.execute("SELECT count(*) FROM paper_order_proposals").fetchone()[0] == 1
        assert ledger.conn.execute("SELECT count(*) FROM paper_risk_reviews").fetchone()[0] == 1
        assert ledger.conn.execute("SELECT verdict FROM paper_risk_reviews").fetchone()[0] == "REJECT"
        assert ledger.conn.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 0
        assert ledger.conn.execute("SELECT count(*) FROM paper_fills").fetchone()[0] == 0
        assert ledger.cash_balance("acct") == 10_000_000 and ledger.positions("acct") == {}


def test_fills_queues_order_and_only_later_cycle_observation_settles(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    same = MarketObservation("005930", 70_000, CUTOFF, "a" * 64)
    first = run_cycle(_config(path, "fills"), _input(observations=(same,)))
    assert first.counts["orders"] == 1 and first.counts["fills"] == 0
    assert run_cycle(_config(path, "fills"), _input(observations=(same,))).replayed
    later = MarketObservation("005930", 71_000, "2026-08-08T10:01:00+09:00", "b" * 64)
    next_lineage = replace(_lineage(), ksf_run_id="run-next", ksf_decision_id="ksf-next")
    second_input = replace(_input(observations=(later,), lineages=(next_lineage,)),
        session_id="2026-08-09", cycle_at="2026-08-09T10:00:00+09:00")
    second = run_cycle(_config(path, "fills"), second_input)
    assert second.counts["fills"] == 1


def test_two_symbol_cycle_after_prior_fill_commits_both_decisions_without_rollback(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    first_obs = MarketObservation("005930", 70_000, CUTOFF, "a" * 64)
    run_cycle(_config(path, "fills"), _input(observations=(first_obs,)))
    later_at = "2026-08-09T09:59:00+09:00"
    lineages = tuple(_lineage(symbol, _snapshot(symbol)) for symbol in ("000660", "005930"))
    lineages = tuple(replace(item, ksf_run_id="next-" + item.symbol,
        ksf_decision_id="next-k-" + item.symbol) for item in lineages)
    observations = tuple(MarketObservation(symbol, 71_000, later_at, ("b" if symbol == "005930" else "c") * 64)
        for symbol in ("000660", "005930"))
    data = replace(_input(lineages=lineages, observations=observations),
        session_id="2026-08-09", cycle_at="2026-08-09T10:00:00+09:00")
    result = run_cycle(_config(path, "fills"), data)
    assert result.committed and result.counts["fills"] == 1 and result.counts["decisions"] == 2


def test_equal_time_fill_nav_does_not_override_explicit_cycle_mark_for_proposals(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    first_obs = MarketObservation("005930", 70_000, CUTOFF, "a" * 64)
    run_cycle(_config(path, "fills"), _input(observations=(first_obs,)))
    cycle_at = "2026-08-09T10:00:00+09:00"
    lineages = tuple(replace(_lineage(symbol), ksf_run_id="tie-" + symbol,
        ksf_decision_id="tie-k-" + symbol) for symbol in ("000660", "005930"))
    captured = {}
    base_transport = _input().transport
    def capture(request):
        captured[request["symbol"]] = request["holdings"]
        return base_transport(request)
    data = replace(_input(lineages=lineages, observations=(MarketObservation(
        "005930", 71_000, cycle_at, "b" * 64),), transport=capture),
        session_id="2026-08-09", cycle_at=cycle_at)

    result = run_cycle(_config(path, "fills"), data)

    assert result.committed and result.counts["fills"] == 1 and result.counts["decisions"] == 2
    with PaperLedger(path) as ledger:
        fill_id = ledger.conn.execute(
            "SELECT fill_id FROM paper_fills WHERE observed_at=?", (cycle_at,)
        ).fetchone()[0]
        cycle_id = ledger.conn.execute(
            "SELECT cycle_id FROM paper_cycle_commits WHERE session_id=?", (data.session_id,)
        ).fetchone()[0]
        nav_ids = {row[0] for row in ledger.conn.execute(
            "SELECT nav_snapshot_id FROM paper_nav_snapshots WHERE nav_snapshot_id IN (?, ?)",
            (fill_id + ":nav", cycle_id + ":nav"),
        )}
    assert nav_ids == {fill_id + ":nav", cycle_id + ":nav"}
    samsung = captured["005930"]
    hynix = captured["000660"]
    assert samsung["market_value_krw"] == samsung["quantity"] * 70_000
    assert samsung["other_position_value_krw"] == 0
    assert hynix["market_value_krw"] == 0
    assert hynix["other_position_value_krw"] == samsung["market_value_krw"]
    for holdings in captured.values():
        assert (holdings["cash_krw"] + holdings["market_value_krw"]
            + holdings["other_position_value_krw"] == holdings["total_equity_krw"])


def test_missing_close_for_authoritative_position_raises_ledger_error(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    first_obs = MarketObservation("005930", 70_000, CUTOFF, "a" * 64)
    run_cycle(_config(path, "fills"), _input(observations=(first_obs,)))
    cycle_at = "2026-08-09T10:00:00+09:00"
    snapshot = _snapshot(omit={"close"})
    data = replace(_input(lineages=(_lineage(snapshot=snapshot),)),
        session_id="2026-08-09", cycle_at=cycle_at)

    with pytest.raises(LedgerError, match="close"):
        run_cycle(_config(path, "shadow"), data)


def test_shadow_with_existing_position_persists_other_symbol_decision(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    first_obs = MarketObservation("005930", 70_000, CUTOFF, "a" * 64)
    run_cycle(_config(path, "fills"), _input(observations=(first_obs,)))
    later_at = "2026-08-09T09:59:00+09:00"
    both = tuple(replace(_lineage(symbol), ksf_run_id="shadow-" + symbol,
        ksf_decision_id="shadow-k-" + symbol) for symbol in ("000660", "005930"))
    settle = replace(_input(lineages=both, observations=(MarketObservation(
        "005930", 71_000, later_at, "b" * 64),)), session_id="2026-08-09",
        cycle_at="2026-08-09T10:00:00+09:00")
    run_cycle(_config(path, "fills"), settle)
    third = tuple(replace(_lineage(symbol), ksf_run_id="third-" + symbol,
        ksf_decision_id="third-k-" + symbol) for symbol in ("000660", "005930"))
    shadow = replace(_input(lineages=third), session_id="2026-08-10",
        cycle_at="2026-08-10T10:00:00+09:00")
    result = run_cycle(_config(path, "shadow"), shadow)
    assert result.committed and result.counts["decisions"] == 2
    with PaperLedger(path) as ledger:
        assert ledger.conn.execute("SELECT count(*) FROM paper_agent_decisions WHERE symbol='000660'").fetchone()[0] >= 1


def test_failure_after_actual_settlement_rolls_back_fill_economics_and_cycle(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    first_obs = MarketObservation("005930", 70_000, CUTOFF, "a" * 64)
    run_cycle(_config(path, "fills"), _input(observations=(first_obs,)))
    with PaperLedger(path) as ledger:
        order_id = ledger.conn.execute("SELECT order_id FROM paper_orders").fetchone()[0]
        before = (ledger.cash_balance("acct"), ledger.positions("acct"),
            ledger.conn.execute("SELECT count(*) FROM paper_fills").fetchone()[0],
            ledger.conn.execute("SELECT count(*) FROM paper_nav_snapshots").fetchone()[0],
            ledger.conn.execute("SELECT count(*) FROM paper_cycle_commits").fetchone()[0])
    later = MarketObservation("005930", 71_000, "2026-08-09T09:59:00+09:00", "b" * 64)
    next_input = replace(_input(observations=(later,), lineages=(replace(_lineage(),
        ksf_run_id="next", ksf_decision_id="next-d"),)), session_id="2026-08-09",
        cycle_at="2026-08-09T10:00:00+09:00")
    with pytest.raises(RuntimeError, match="after execute"):
        run_cycle(_config(path, "fills"), next_input,
            fault_hook=lambda stage: (_ for _ in ()).throw(RuntimeError("after execute")) if stage == "settle" else None)
    with PaperLedger(path) as ledger:
        after = (ledger.cash_balance("acct"), ledger.positions("acct"),
            ledger.conn.execute("SELECT count(*) FROM paper_fills").fetchone()[0],
            ledger.conn.execute("SELECT count(*) FROM paper_nav_snapshots").fetchone()[0],
            ledger.conn.execute("SELECT count(*) FROM paper_cycle_commits").fetchone()[0])
        assert after == before and ledger.order_state(order_id) == "ACCEPTED"


@pytest.mark.parametrize("state", ["disabled", "missing-kill", "future-kill"])
def test_invalid_authoritative_state_prevents_all_cycle_mutation(tmp_path, state):
    path = tmp_path / f"{state}.sqlite3"
    if state != "missing-kill":
        _seed_account(path)
    else:
        ledger = PaperLedger(path)
        ledger.create_account(account_id="acct", initial_cash_krw=10_000_000,
            policy_version="v1", created_at="2026-08-08T08:00:00+09:00")
        ledger.append_account_event(account_event_id="enabled", account_id="acct",
            event_type="ENABLED", event_at="2026-08-08T08:01:00+09:00", reason_code="MANUAL")
        ledger.close()
    with PaperLedger(path) as ledger:
        if state == "disabled":
            ledger.append_account_event(account_event_id="off", account_id="acct", event_type="DISABLED",
                event_at="2026-08-08T09:00:00+09:00", reason_code="MANUAL")
        elif state != "missing-kill":
            ledger.append_kill_switch_event(kill_switch_event_id="future", account_id="acct",
                event_type="RELEASED", reason_code="MANUAL", event_at="2026-08-09T08:00:00+09:00")
        before = ledger.conn.total_changes
    with pytest.raises(LedgerError): run_cycle(_config(path, "fills"), _input())
    with PaperLedger(path) as ledger:
        assert ledger.conn.execute("SELECT count(*) FROM paper_agent_decisions").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["settle", "mark", "agent", "risk", "order"])
def test_failure_at_every_stage_rolls_back_entire_cycle(tmp_path, stage):
    path = tmp_path / f"{stage}.sqlite3"; _seed_account(path)
    with pytest.raises(RuntimeError, match="fault"):
        run_cycle(_config(path, "fills"), _input(), fault_hook=lambda current: (_ for _ in ()).throw(RuntimeError("fault")) if current == stage else None)
    with PaperLedger(path) as ledger:
        assert ledger.conn.execute("SELECT count(*) FROM paper_agent_decisions").fetchone()[0] == 0
        assert ledger.conn.execute("SELECT count(*) FROM paper_nav_snapshots").fetchone()[0] == 0


def test_alert_failure_is_after_commit_and_digest_is_sanitized(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    def fail(_digest): raise RuntimeError("secret /srv prompt rationale")
    result = run_cycle(_config(path), _input(), alert=fail)
    assert result.committed and result.alert_error == "ALERT_FAILED"
    forbidden = ("prompt", "rationale", "/srv", "secret", "fixture-v1")
    assert not any(word in result.sanitized_digest.lower() for word in forbidden)


def test_exact_replay_noop_conflicting_identity_fails_closed(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    first = run_cycle(_config(path), _input()); second = run_cycle(_config(path), _input())
    assert first.committed and second.replayed and first.counts == second.counts
    changed = _input(lineages=(_lineage(snapshot=_snapshot(omit={"volume"})),))
    with pytest.raises(LedgerConflictError): run_cycle(_config(path), changed)


@pytest.mark.parametrize("change", ["provider", "model", "bundle", "risk", "execution"])
def test_replay_identity_binds_model_bundle_and_policies(tmp_path, change):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    config, data = _config(path), _input()
    run_cycle(config, data)
    if change == "provider": data = replace(data, model_provider="other")
    elif change == "model": data = replace(data, model_name="other")
    elif change == "bundle": data = replace(data, response_bundle_sha256="d" * 64)
    elif change == "risk": config = replace(config, risk_policy=replace(config.risk_policy, daily_loss_stop_krw=1))
    else: config = replace(config, execution_policy=replace(config.execution_policy, fee_bps=1))
    with pytest.raises(LedgerConflictError):
        run_cycle(config, data)


def test_replay_counts_are_cycle_local_and_alert_is_not_resent(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    first_alerts, replay_alerts = [], []
    first = run_cycle(_config(path), _input(), alert=first_alerts.append)
    replay = run_cycle(_config(path), _input(), alert=replay_alerts.append)
    assert first.committed and not first.replayed
    assert not replay.committed and replay.replayed
    assert replay.counts == first.counts == {"decisions": 1, "proposals": 1, "reviews": 1, "orders": 0, "fills": 0}
    assert replay.sanitized_digest.startswith("paper-cycle status=replayed")
    assert len(first_alerts) == 1 and replay_alerts == []


def test_exit_maps_to_negative_flow_reason(tmp_path, monkeypatch):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    monkeypatch.setattr("strategy.paper_cycle.propose", lambda *_args, **_kwargs: {
        "action": "EXIT", "target_exposure_pct": 0.0, "abstain_reason": None})
    run_cycle(_config(path), _input())
    with PaperLedger(path) as ledger:
        assert ledger.conn.execute("SELECT reason_code FROM paper_agent_decisions").fetchone()[0] == "FLOW_SIGNAL_NEGATIVE"


def test_authoritative_prior_session_nav_drives_daily_loss_stop(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    with PaperLedger(path) as ledger:
        ledger.append_nav_snapshot(nav_snapshot_id="prior", account_id="acct", session_date="2026-08-07",
            cash_krw=10_000_000, position_value_krw=0, nav_krw=10_000_000,
            position_marks={}, snapshot_at="2026-08-07T15:30:00+09:00")
        prior_cutoff = "2026-08-07T15:29:00+09:00"
        ledger.append_agent_decision(decision_id="loss-d", account_id="acct", symbol="005930", ksf_run_id="loss-r", ksf_decision_id="loss-k", feature_snapshot_sha256="a"*64, available_data_cutoff=prior_cutoff, action="ENTER", reason_code="FLOW_SIGNAL_POSITIVE", rationale_sha256="b"*64, created_at="2026-08-07T15:30:00+09:00")
        ledger.append_order_proposal(proposal_id="loss-p", decision_id="loss-d", side="BUY", target_exposure_bp=1000, idempotency_key="loss-p", model_name="m", model_version="v", proposed_at="2026-08-07T15:30:00+09:00")
        ledger.append_risk_review(review_id="loss-rv", proposal_id="loss-p", verdict="APPROVE", approved_quantity=10, reject_reason_code=None, reference_price_krw=70_000, policy_sha256="c"*64, reviewed_at="2026-08-07T15:30:00+09:00")
        ledger.append_order(order_id="loss-o", review_id="loss-rv", idempotency_key="loss-o", created_at="2026-08-07T15:30:00+09:00")
        ledger.append_fill(fill_id="loss-f", order_id="loss-o", raw_reference_price_krw=70_000,
            fill_price_krw=70_000, quantity=10, fee_krw=300_000, tax_krw=0, slippage_krw=0,
            fee_bps=0, tax_bps=0, slippage_bps=0, execution_policy_sha256="d"*64,
            observed_at="2026-08-08T09:00:00+09:00", observation_sha256="e"*64,
            filled_at="2026-08-08T09:00:00+09:00")
    config = replace(_config(path), risk_policy=replace(_config(path).risk_policy, daily_loss_stop_krw=200_000))
    run_cycle(config, _input())
    with PaperLedger(path) as ledger:
        assert ledger.conn.execute("SELECT verdict,reject_reason_code FROM paper_risk_reviews WHERE proposal_id!='loss-p'").fetchone()[:] == ("REJECT", "DAILY_LOSS_STOP")


def test_settlement_is_account_scoped(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    with PaperLedger(path) as ledger:
        ledger.create_account(account_id="other", initial_cash_krw=10_000_000, policy_version="v1", created_at="2026-08-08T08:00:00+09:00")
        ledger.append_account_event(account_event_id="other-enabled", account_id="other", event_type="ENABLED", event_at="2026-08-08T08:01:00+09:00", reason_code="MANUAL")
        ledger.append_kill_switch_event(kill_switch_event_id="other-kill", account_id="other", event_type="ENGAGED", reason_code="MANUAL", event_at="2026-08-08T08:02:00+09:00")
        # A real accepted B order is easiest to create through the proven ledger append API.
        ledger.append_agent_decision(decision_id="d-other", account_id="other", symbol="005930", ksf_run_id="r", ksf_decision_id="k", feature_snapshot_sha256="a"*64, available_data_cutoff=CUTOFF, action="ENTER", reason_code="FLOW_SIGNAL_POSITIVE", rationale_sha256="b"*64, created_at=NOW)
        ledger.append_order_proposal(proposal_id="p-other", decision_id="d-other", side="BUY", target_exposure_bp=100, idempotency_key="p-other", model_name="m", model_version="v", proposed_at=NOW)
        ledger.append_risk_review(review_id="rv-other", proposal_id="p-other", verdict="APPROVE", approved_quantity=1, reject_reason_code=None, reference_price_krw=70_000, policy_sha256="c"*64, reviewed_at=NOW)
        ledger.append_order(order_id="o-other", review_id="rv-other", idempotency_key="o-other", created_at=NOW)
    obs = MarketObservation("005930", 70_000, CUTOFF, "a" * 64)
    run_cycle(_config(path, "fills"), _input(observations=(obs,)))
    with PaperLedger(path) as ledger:
        assert ledger.order_state("o-other") == "ACCEPTED"
        assert ledger.conn.execute("SELECT count(*) FROM paper_fills WHERE account_id='other'").fetchone()[0] == 0


def test_incomplete_ksf_abstains_before_transport(tmp_path):
    path = tmp_path / "incomplete.sqlite3"; _seed_account(path)
    snapshot = _snapshot(omit={"volume"}); called = []
    result = run_cycle(_config(path), _input(lineages=(_lineage(snapshot=snapshot),),
        transport=lambda _request: called.append(True)))
    assert result.committed and called == []
    with PaperLedger(path) as ledger:
        row = ledger.conn.execute("SELECT action,reason_code FROM paper_agent_decisions").fetchone()
        assert tuple(row) == ("ABSTAIN", "DATA_MISSING")


@pytest.mark.parametrize("kind", ["future", "cross-symbol", "malformed-hash"])
def test_future_cross_symbol_and_malformed_lineage_fail_before_mutation(tmp_path, kind):
    path = tmp_path / f"{kind}.sqlite3"; _seed_account(path)
    lineage = _lineage()
    if kind == "future": lineage = replace(lineage, available_data_cutoff="2026-08-09T10:00:00+09:00")
    elif kind == "cross-symbol": lineage = replace(lineage, symbol="000660")
    else: lineage = replace(lineage, feature_snapshot_sha256="0" * 64)
    with pytest.raises(LedgerError): run_cycle(_config(path), _input(lineages=(lineage,)))
    with PaperLedger(path) as ledger:
        assert ledger.conn.execute("SELECT count(*) FROM paper_cycle_commits").fetchone()[0] == 0


def test_lock_contention_skips_without_opening_ledger(tmp_path):
    config = _config(tmp_path / "absent.sqlite3")
    class Busy:
        def __enter__(self): return False
        def __exit__(self, *args): pass
    result = run_cycle(config, _input(), lock_factory=lambda _: Busy())
    assert result.skipped_lock and not config.paper_db_path.exists()


def test_concurrent_duplicate_cycles_have_one_audit_and_economic_mutation(tmp_path):
    path = tmp_path / "paper.sqlite3"; _seed_account(path)
    entered, release, results = threading.Event(), threading.Event(), []
    base = _input()
    def slow(request):
        entered.set(); release.wait(2)
        return base.transport(request)
    data = replace(base, transport=slow)
    worker = threading.Thread(target=lambda: results.append(run_cycle(_config(path, "fills"), data)))
    worker.start(); assert entered.wait(2)
    results.append(run_cycle(_config(path, "fills"), data))
    release.set(); worker.join(2)
    assert len(results) == 2 and sum(r.skipped_lock for r in results) == 1
    with PaperLedger(path) as ledger:
        assert ledger.conn.execute("SELECT count(*) FROM paper_cycle_commits").fetchone()[0] == 1
        assert ledger.conn.execute("SELECT count(*) FROM paper_agent_decisions").fetchone()[0] == 1
        assert ledger.conn.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 1


def test_runtime_path_prohibits_live_broker_order_adapters_and_process_execution():
    root = Path(__file__).resolve().parents[1]
    forbidden_modules = {"subprocess", "requests", "httpx", "pykis", "kis",
        "strategy.paper_trader", "broker", "order_adapter"}
    forbidden_calls = {"system", "popen", "run", "call", "check_call", "check_output"}
    for relative in ("strategy/paper_cycle.py", "strategy/paper_source.py"):
        tree = ast.parse((root / relative).read_text())
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        imports |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        assert not imports & forbidden_modules
        assert not calls & forbidden_calls
