from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "001_k_semiconductor_flow_desk_permanent_ledgers.sql"
UPGRADE = Path(__file__).resolve().parents[1] / "migrations" / "002_k_semiconductor_flow_desk_update_guard_upgrade.sql"


def apply_migration(conn: sqlite3.Connection) -> None:
    conn.executescript(MIGRATION.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")


def apply_upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript(UPGRADE.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")


WEAK_LEGACY_TRIGGERS = """
DROP TRIGGER trg_ksf_decisions_append_only;
CREATE TRIGGER trg_ksf_decisions_append_only
BEFORE UPDATE ON ksf_decisions
FOR EACH ROW
WHEN 0
BEGIN
    SELECT RAISE(ABORT, 'legacy weak guard never fires');
END;
DROP TRIGGER trg_ksf_source_snapshots_provenance_immutable;
CREATE TRIGGER trg_ksf_source_snapshots_provenance_immutable
BEFORE UPDATE ON ksf_source_snapshots
FOR EACH ROW
WHEN 0
BEGIN
    SELECT RAISE(ABORT, 'legacy weak guard never fires');
END;
"""


def simulate_pre_remediation_db(conn: sqlite3.Connection) -> None:
    """001 적용 후 같은 이름의 약한(legacy) trigger 정의만 남은 기존 영구 DB 를 재현한다."""
    conn.executescript(WEAK_LEGACY_TRIGGERS)
    # Rerunning 001 must NOT repair them: CREATE TRIGGER IF NOT EXISTS keeps the weak same-name
    # definitions. This is exactly the reported production gap that 002 exists to close.
    apply_migration(conn)


@pytest.fixture
def ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_migration(conn)
    yield conn
    conn.close()


def insert_metadata(conn: sqlite3.Connection, metadata_id: str, source_name: str) -> None:
    conn.execute(
        """
        INSERT INTO ksf_collected_source_metadata
        (metadata_id, source_name, capture_date, quality_grade, raw_storage_policy)
        VALUES (?, ?, '2026-08-01', 'A', 'METADATA_ONLY')
        """,
        (metadata_id, source_name),
    )


def insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    symbol: str,
    *,
    as_of: str = "2026-08-01T16:00:00+09:00",
    cutoff: str = "2026-08-01T15:59:00+09:00",
    trading_date: str = "2026-08-01",
) -> None:
    conn.execute(
        """
        INSERT INTO ksf_runs
        (run_id, symbol, trading_date, run_status, as_of_kst, available_data_cutoff)
        VALUES (?, ?, ?, 'READY', ?, ?)
        """,
        (run_id, symbol, trading_date, as_of, cutoff),
    )


def insert_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    metadata_id: str | None,
    source_name: str = "fixture_source",
    run_id: str | None = None,
    symbol: str | None = None,
    source_as_of: str = "2026-08-01T06:58:00+00:00",
    ingested_at: str = "2026-08-01T15:58:30+09:00",
    cutoff: str = "2026-08-01T15:59:00+09:00",
) -> None:
    conn.execute(
        """
        INSERT INTO ksf_source_snapshots
        (snapshot_id, run_id, symbol, source_metadata_id, source_name, source_kind,
         source_status, source_as_of, ingested_at_kst, available_data_cutoff,
         quality_grade)
        VALUES (?, ?, ?, ?, ?, 'global_price', 'CLOSE_CONFIRMED', ?, ?, ?, 'A')
        """,
        (snapshot_id, run_id, symbol, metadata_id, source_name, source_as_of, ingested_at, cutoff),
    )


def insert_feature(
    conn: sqlite3.Connection,
    feature_id: str,
    run_id: str,
    symbol: str,
    snapshot_id: str,
    *,
    cutoff: str = "2026-08-01T15:59:00+09:00",
) -> None:
    conn.execute(
        """
        INSERT INTO ksf_normalized_features
        (feature_id, run_id, symbol, source_snapshot_id, feature_group, feature_name,
         feature_version, feature_status, value_num, source_as_of, ingested_at_kst,
         available_data_cutoff)
        VALUES (?, ?, ?, ?, 'test', ?, 'v1', 'READY', 1.0,
                '2026-08-01T06:58:00+00:00', '2026-08-01T15:58:30+09:00', ?)
        """,
        (feature_id, run_id, symbol, snapshot_id, feature_id, cutoff),
    )


def insert_decision(
    conn: sqlite3.Connection,
    decision_id: str,
    run_id: str,
    symbol: str,
    *,
    horizon: int = 1,
    cutoff: str = "2026-08-01T15:59:00+09:00",
) -> None:
    conn.execute(
        """
        INSERT INTO ksf_decisions
        (decision_id, run_id, symbol, horizon_days, as_of_kst, available_data_cutoff,
         score_label, user_opinion, scoring_ruleset_version, feature_snapshot_sha256)
        VALUES (?, ?, ?, ?, '2026-08-01T16:00:00+09:00', ?, 'neutral_watch', 'NEUTRAL', 'v1', 'feat-hash')
        """,
        (decision_id, run_id, symbol, horizon, cutoff),
    )


def insert_ai_request(
    conn: sqlite3.Connection,
    ai_request_id: str,
    run_id: str,
    symbol: str,
    *,
    cutoff: str = "2026-08-01T15:59:00+09:00",
    prompt_hash: str = "prompt-hash",
) -> None:
    conn.execute(
        """
        INSERT INTO ksf_ai_requests
        (ai_request_id, run_id, symbol, purpose, as_of_kst, available_data_cutoff,
         prompt_template_version, redaction_policy_version, input_ledger_hash_sha256,
         prompt_hash_sha256, model_provider, model_name)
        VALUES (?, ?, ?, 'explain_decision', '2026-08-01T16:00:00+09:00', ?,
                'v1', 'v1', 'in-hash', ?, 'anthropic', 'claude-fable-5')
        """,
        (ai_request_id, run_id, symbol, cutoff, prompt_hash),
    )


def insert_ai_response(
    conn: sqlite3.Connection,
    ai_response_id: str,
    ai_request_id: str,
    run_id: str,
    symbol: str,
) -> None:
    conn.execute(
        """
        INSERT INTO ksf_ai_responses
        (ai_response_id, ai_request_id, run_id, symbol, response_status, response_hash_sha256,
         model_provider, model_name)
        VALUES (?, ?, ?, ?, 'OK', 'resp-hash', 'anthropic', 'claude-fable-5')
        """,
        (ai_response_id, ai_request_id, run_id, symbol),
    )


def insert_settlement(
    conn: sqlite3.Connection,
    settlement_id: str,
    decision_id: str,
    run_id: str,
    symbol: str,
    *,
    horizon: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO ksf_performance_settlements
        (settlement_id, decision_id, run_id, symbol, horizon_days, base_trade_date,
         settlement_status, due_after_kst)
        VALUES (?, ?, ?, ?, ?, '2026-08-01', 'PENDING_SETTLEMENT', '2026-08-04T16:00:00+09:00')
        """,
        (settlement_id, decision_id, run_id, symbol, horizon),
    )


def test_fresh_migration_reapplies_idempotently_and_is_valid():
    conn = sqlite3.connect(":memory:")
    apply_migration(conn)
    apply_migration(conn)

    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT version, name FROM ksf_schema_versions").fetchall() == [
        (1, "001_k_semiconductor_flow_desk_permanent_ledgers")
    ]
    assert conn.execute("SELECT symbol FROM ksf_symbols ORDER BY symbol").fetchall() == [("000660",), ("005930",)]


def test_connection_backup_round_trip_preserves_ledger(tmp_path: Path):
    source = sqlite3.connect(":memory:")
    apply_migration(source)
    backup_path = tmp_path / "ledger-backup.sqlite3"
    destination = sqlite3.connect(backup_path)
    source.backup(destination)
    destination.execute("PRAGMA foreign_keys = ON")

    assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert destination.execute("PRAGMA foreign_key_check").fetchall() == []
    assert destination.execute("SELECT version FROM ksf_schema_versions").fetchall() == [(1,)]
    assert destination.execute("SELECT symbol FROM ksf_symbols ORDER BY symbol").fetchall() == [("000660",), ("005930",)]


def test_snapshot_requires_existing_matching_source_metadata(ledger: sqlite3.Connection):
    insert_metadata(ledger, "meta_fixture", "fixture_source")

    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(ledger, "snap_null", metadata_id=None)
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(ledger, "snap_missing", metadata_id="meta_missing")
    with pytest.raises(sqlite3.IntegrityError, match="metadata source_name"):
        insert_snapshot(ledger, "snap_wrong_source", metadata_id="meta_fixture", source_name="other_source")

    insert_snapshot(ledger, "snap_valid", metadata_id="meta_fixture")
    assert ledger.execute(
        "SELECT source_metadata_id FROM ksf_source_snapshots WHERE snapshot_id='snap_valid'"
    ).fetchone() == ("meta_fixture",)
    with pytest.raises(sqlite3.IntegrityError, match="metadata source_name|immutable"):
        ledger.execute("UPDATE ksf_source_snapshots SET source_name='other_source' WHERE snapshot_id='snap_valid'")


def test_feature_lineage_allows_only_global_or_same_symbol_snapshot(ledger: sqlite3.Connection):
    insert_metadata(ledger, "meta_fixture", "fixture_source")
    insert_run(ledger, "run_samsung", "005930")
    insert_snapshot(ledger, "snap_global", metadata_id="meta_fixture")
    insert_snapshot(ledger, "snap_hynix", metadata_id="meta_fixture", symbol="000660")

    insert_feature(ledger, "feature_global", "run_samsung", "005930", "snap_global")
    with pytest.raises(sqlite3.IntegrityError, match="snapshot lineage"):
        insert_feature(ledger, "feature_cross_symbol", "run_samsung", "005930", "snap_hynix")


def test_feature_lineage_rejects_cross_run_snapshot(ledger: sqlite3.Connection):
    insert_metadata(ledger, "meta_fixture", "fixture_source")
    insert_run(ledger, "run_one", "005930", cutoff="2026-08-01T15:58:00+09:00")
    insert_run(ledger, "run_two", "005930")
    insert_snapshot(
        ledger,
        "snap_run_one",
        metadata_id="meta_fixture",
        run_id="run_one",
        symbol="005930",
        ingested_at="2026-08-01T15:57:30+09:00",
        cutoff="2026-08-01T15:58:00+09:00",
    )

    with pytest.raises(sqlite3.IntegrityError, match="snapshot lineage"):
        insert_feature(
            ledger,
            "feature_cross_run",
            "run_two",
            "005930",
            "snap_run_one",
            cutoff="2026-08-01T15:59:00+09:00",
        )


def test_run_cutoff_compares_absolute_instants_and_requires_canonical_kst(ledger: sqlite3.Connection):
    with pytest.raises(sqlite3.IntegrityError):
        insert_run(
            ledger,
            "future_cutoff",
            "005930",
            as_of="2026-08-01T16:00:00+09:00",
            cutoff="2026-08-01T08:00:00+00:00",
        )

    insert_run(ledger, "normal_cutoff", "005930")

    for malformed in ("not-a-time", "2026-08-01T16:00:00", "2026-08-01T16:00:00+00:00"):
        with pytest.raises(sqlite3.IntegrityError):
            insert_run(ledger, f"bad_{len(malformed)}_{malformed[-2:]}", "000660", as_of=malformed)


def test_run_identity_update_aborts_while_status_lifecycle_survives(ledger: sqlite3.Connection):
    insert_run(ledger, "run_samsung", "005930")

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_runs SET symbol='000660' WHERE run_id='run_samsung'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_runs SET trading_date='2026-08-02' WHERE run_id='run_samsung'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute(
            "UPDATE ksf_runs SET available_data_cutoff='2026-08-01T15:58:00+09:00' WHERE run_id='run_samsung'"
        )

    ledger.execute("UPDATE ksf_runs SET run_status='SCORING_DONE' WHERE run_id='run_samsung'")
    assert ledger.execute(
        "SELECT run_status FROM ksf_runs WHERE run_id='run_samsung'"
    ).fetchone() == ("SCORING_DONE",)


def test_run_provenance_versions_and_timestamps_are_immutable(ledger: sqlite3.Connection):
    insert_run(ledger, "run_samsung", "005930")

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_runs SET scoring_ruleset_version='v2' WHERE run_id='run_samsung'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_runs SET prompt_template_version='v2' WHERE run_id='run_samsung'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_runs SET model_policy_version='v2' WHERE run_id='run_samsung'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_runs SET as_of_kst='2026-08-01T17:00:00+09:00' WHERE run_id='run_samsung'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_runs SET created_at_kst='2026-08-01T00:00:00+09:00' WHERE run_id='run_samsung'")

    ledger.execute(
        "UPDATE ksf_runs SET run_status='ARCHIVED', archived_at_kst='2026-08-02T09:00:00+09:00',"
        " archived_reason='superseded' WHERE run_id='run_samsung'"
    )
    assert ledger.execute(
        "SELECT run_status FROM ksf_runs WHERE run_id='run_samsung'"
    ).fetchone() == ("ARCHIVED",)


def test_snapshot_lineage_update_cannot_break_run_relationship(ledger: sqlite3.Connection):
    insert_metadata(ledger, "meta_fixture", "fixture_source")
    insert_run(ledger, "run_samsung", "005930")
    insert_run(ledger, "run_early", "005930", cutoff="2026-08-01T15:50:00+09:00")
    insert_snapshot(
        ledger,
        "snap_linked",
        metadata_id="meta_fixture",
        run_id="run_samsung",
        symbol="005930",
        source_as_of="2026-08-01T06:40:00+00:00",
        ingested_at="2026-08-01T15:49:00+09:00",
    )

    # The reported blocker: cross-symbol snapshot mutation must abort.
    with pytest.raises(sqlite3.IntegrityError, match="must match run|immutable"):
        ledger.execute("UPDATE ksf_source_snapshots SET symbol='000660' WHERE snapshot_id='snap_linked'")
    with pytest.raises(sqlite3.IntegrityError, match="must match run|immutable"):
        ledger.execute(
            "UPDATE ksf_source_snapshots SET available_data_cutoff='2026-08-01T15:59:30+09:00'"
            " WHERE snapshot_id='snap_linked'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="must match run|immutable"):
        ledger.execute("UPDATE ksf_source_snapshots SET run_id='run_early' WHERE snapshot_id='snap_linked'")

    ledger.execute("UPDATE ksf_source_snapshots SET source_status='STALE' WHERE snapshot_id='snap_linked'")
    assert ledger.execute(
        "SELECT source_status, symbol FROM ksf_source_snapshots WHERE snapshot_id='snap_linked'"
    ).fetchone() == ("STALE", "005930")


def test_snapshot_provenance_hashes_links_and_null_detach_are_immutable(ledger: sqlite3.Connection):
    insert_metadata(ledger, "meta_fixture", "fixture_source")
    insert_run(ledger, "run_samsung", "005930")
    # Same symbol/cutoff run: a "consistent" parent swap that the relationship guards alone would allow.
    insert_run(ledger, "run_twin", "005930", trading_date="2026-08-02")
    insert_snapshot(ledger, "snap_linked", metadata_id="meta_fixture", run_id="run_samsung", symbol="005930")

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute(
            "UPDATE ksf_source_snapshots SET normalized_payload_sha256='deadbeef' WHERE snapshot_id='snap_linked'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_source_snapshots SET run_id=NULL WHERE snapshot_id='snap_linked'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_source_snapshots SET symbol=NULL WHERE snapshot_id='snap_linked'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_source_snapshots SET run_id='run_twin' WHERE snapshot_id='snap_linked'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute(
            """UPDATE ksf_source_snapshots SET snapshot_metadata_json='{"tampered":1}' WHERE snapshot_id='snap_linked'"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_source_snapshots SET raw_ref_sha256='deadbeef' WHERE snapshot_id='snap_linked'")

    ledger.execute(
        "UPDATE ksf_source_snapshots SET source_status='STALE', quality_grade='C', lag_days=1"
        " WHERE snapshot_id='snap_linked'"
    )
    assert ledger.execute(
        "SELECT source_status, quality_grade, lag_days FROM ksf_source_snapshots WHERE snapshot_id='snap_linked'"
    ).fetchone() == ("STALE", "C", 1)


def test_global_snapshot_symbol_update_cannot_break_feature_lineage(ledger: sqlite3.Connection):
    insert_metadata(ledger, "meta_fixture", "fixture_source")
    insert_run(ledger, "run_samsung", "005930")
    insert_snapshot(ledger, "snap_global", metadata_id="meta_fixture")
    insert_feature(ledger, "feature_a", "run_samsung", "005930", "snap_global")

    with pytest.raises(sqlite3.IntegrityError, match="feature lineage|immutable"):
        ledger.execute("UPDATE ksf_source_snapshots SET symbol='000660' WHERE snapshot_id='snap_global'")


def test_feature_lineage_update_cannot_break_run_relationship(ledger: sqlite3.Connection):
    insert_metadata(ledger, "meta_fixture", "fixture_source")
    insert_run(ledger, "run_samsung", "005930")
    insert_snapshot(ledger, "snap_global", metadata_id="meta_fixture")
    insert_feature(ledger, "feature_a", "run_samsung", "005930", "snap_global")

    # The global (NULL-symbol) snapshot would not catch this; the run relationship must.
    with pytest.raises(sqlite3.IntegrityError, match="must match run|immutable"):
        ledger.execute("UPDATE ksf_normalized_features SET symbol='000660' WHERE feature_id='feature_a'")
    with pytest.raises(sqlite3.IntegrityError, match="must match run|immutable"):
        ledger.execute(
            "UPDATE ksf_normalized_features SET available_data_cutoff='2026-08-01T15:59:30+09:00'"
            " WHERE feature_id='feature_a'"
        )

    ledger.execute("UPDATE ksf_normalized_features SET feature_status='STALE' WHERE feature_id='feature_a'")
    assert ledger.execute(
        "SELECT feature_status FROM ksf_normalized_features WHERE feature_id='feature_a'"
    ).fetchone() == ("STALE",)


def test_feature_value_payload_and_snapshot_link_are_immutable(ledger: sqlite3.Connection):
    insert_metadata(ledger, "meta_fixture", "fixture_source")
    insert_run(ledger, "run_samsung", "005930")
    insert_run(ledger, "run_twin", "005930", trading_date="2026-08-02")
    insert_snapshot(ledger, "snap_global", metadata_id="meta_fixture")
    insert_feature(ledger, "feature_a", "run_samsung", "005930", "snap_global")

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_normalized_features SET value_num=999.0 WHERE feature_id='feature_a'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_normalized_features SET source_snapshot_id=NULL WHERE feature_id='feature_a'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_normalized_features SET feature_version='v2' WHERE feature_id='feature_a'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_normalized_features SET contribution_cap_bps=0 WHERE feature_id='feature_a'")
    # Consistent parent swap (same symbol/cutoff run) must still abort.
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_normalized_features SET run_id='run_twin' WHERE feature_id='feature_a'")

    ledger.execute(
        "UPDATE ksf_normalized_features SET feature_status='MISSING_OPTIONAL', missing_reason='revoked'"
        " WHERE feature_id='feature_a'"
    )
    assert ledger.execute(
        "SELECT feature_status, missing_reason FROM ksf_normalized_features WHERE feature_id='feature_a'"
    ).fetchone() == ("MISSING_OPTIONAL", "revoked")


def test_decision_lineage_update_aborts_and_content_updates_survive(ledger: sqlite3.Connection):
    insert_run(ledger, "run_samsung", "005930")
    insert_run(ledger, "run_hynix", "000660")
    insert_decision(ledger, "decision_1", "run_samsung", "005930", horizon=1)

    with pytest.raises(sqlite3.IntegrityError, match="must match run|append-only"):
        ledger.execute("UPDATE ksf_decisions SET symbol='000660' WHERE decision_id='decision_1'")
    with pytest.raises(sqlite3.IntegrityError, match="must match run|append-only"):
        ledger.execute("UPDATE ksf_decisions SET run_id='run_hynix' WHERE decision_id='decision_1'")

    insert_settlement(ledger, "settle_1", "decision_1", "run_samsung", "005930", horizon=1)
    with pytest.raises(sqlite3.IntegrityError, match="settlement lineage|append-only"):
        ledger.execute("UPDATE ksf_decisions SET horizon_days=5 WHERE decision_id='decision_1'")

    # The deterministic decision payload is append-only: corrections must add a new run/decision.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.execute("UPDATE ksf_decisions SET feature_snapshot_sha256='tampered' WHERE decision_id='decision_1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.execute("UPDATE ksf_decisions SET deterministic_score=10 WHERE decision_id='decision_1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.execute("""UPDATE ksf_decisions SET rationale_json='{"note":"ok"}' WHERE decision_id='decision_1'""")


def test_ai_request_response_lineage_updates_abort_and_content_updates_survive(ledger: sqlite3.Connection):
    insert_run(ledger, "run_samsung", "005930")
    insert_ai_request(ledger, "request_1", "run_samsung", "005930")
    insert_ai_response(ledger, "response_1", "request_1", "run_samsung", "005930")

    with pytest.raises(sqlite3.IntegrityError, match="must match request|append-only"):
        ledger.execute("UPDATE ksf_ai_responses SET symbol='000660' WHERE ai_response_id='response_1'")
    with pytest.raises(sqlite3.IntegrityError, match="must match request|append-only"):
        ledger.execute("UPDATE ksf_ai_responses SET model_name='other-model' WHERE ai_response_id='response_1'")
    with pytest.raises(sqlite3.IntegrityError, match="must match run|response lineage|append-only"):
        ledger.execute("UPDATE ksf_ai_requests SET symbol='000660' WHERE ai_request_id='request_1'")
    with pytest.raises(sqlite3.IntegrityError, match="response lineage|append-only"):
        ledger.execute("UPDATE ksf_ai_requests SET model_name='other-model' WHERE ai_request_id='request_1'")

    # AI request/response semantic payloads and hashes are append-only after insert.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.execute("UPDATE ksf_ai_requests SET prompt_hash_sha256='tampered' WHERE ai_request_id='request_1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.execute("""UPDATE ksf_ai_requests SET request_metadata_json='{"k":1}' WHERE ai_request_id='request_1'""")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.execute("UPDATE ksf_ai_responses SET response_hash_sha256='tampered' WHERE ai_response_id='response_1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.execute("UPDATE ksf_ai_responses SET summary='rewritten summary' WHERE ai_response_id='response_1'")

    # Consistent parent swap (same run/symbol/model twin request) must also abort.
    insert_ai_request(ledger, "request_twin", "run_samsung", "005930", prompt_hash="prompt-hash-2")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.execute("UPDATE ksf_ai_responses SET ai_request_id='request_twin' WHERE ai_response_id='response_1'")


def test_settlement_lineage_update_aborts_and_settle_lifecycle_survives(ledger: sqlite3.Connection):
    insert_run(ledger, "run_samsung", "005930")
    insert_decision(ledger, "decision_1", "run_samsung", "005930", horizon=1)
    insert_settlement(ledger, "settle_1", "decision_1", "run_samsung", "005930", horizon=1)

    with pytest.raises(sqlite3.IntegrityError, match="must match decision|immutable"):
        ledger.execute("UPDATE ksf_performance_settlements SET symbol='000660' WHERE settlement_id='settle_1'")
    with pytest.raises(sqlite3.IntegrityError, match="must match decision|immutable"):
        ledger.execute("UPDATE ksf_performance_settlements SET horizon_days=5 WHERE settlement_id='settle_1'")

    # Baseline/due/created provenance is immutable even when the decision link stays consistent.
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute("UPDATE ksf_performance_settlements SET base_trade_date='2026-07-31' WHERE settlement_id='settle_1'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute(
            "UPDATE ksf_performance_settlements SET due_after_kst='2026-08-05T16:00:00+09:00' WHERE settlement_id='settle_1'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute(
            "UPDATE ksf_performance_settlements SET created_at_kst='2026-08-01T00:00:00+09:00' WHERE settlement_id='settle_1'"
        )

    # Consistent parent swap to a twin decision (matching run/symbol/horizon) must also abort.
    insert_run(ledger, "run_twin", "005930", trading_date="2026-08-02")
    insert_decision(ledger, "decision_twin", "run_twin", "005930", horizon=1)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.execute(
            "UPDATE ksf_performance_settlements SET decision_id='decision_twin', run_id='run_twin'"
            " WHERE settlement_id='settle_1'"
        )

    ledger.execute(
        """
        UPDATE ksf_performance_settlements
        SET settlement_status='SETTLED', target_trade_date='2026-08-04', base_close=80000.0,
            target_close=81000.0, return_bps=125.0, settled_at_kst='2026-08-04T16:30:00+09:00'
        WHERE settlement_id='settle_1'
        """
    )
    assert ledger.execute(
        "SELECT settlement_status, return_bps FROM ksf_performance_settlements WHERE settlement_id='settle_1'"
    ).fetchone() == ("SETTLED", 125.0)


def test_ordered_migrations_reapply_idempotently_with_exact_version_rows():
    conn = sqlite3.connect(":memory:")
    apply_migration(conn)
    apply_upgrade(conn)
    apply_migration(conn)
    apply_upgrade(conn)

    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT version, name FROM ksf_schema_versions ORDER BY version").fetchall() == [
        (1, "001_k_semiconductor_flow_desk_permanent_ledgers"),
        (2, "002_k_semiconductor_flow_desk_update_guard_upgrade"),
    ]


def test_upgrade_replaces_weak_legacy_same_name_triggers_with_strict_definitions():
    conn = sqlite3.connect(":memory:")
    apply_migration(conn)
    simulate_pre_remediation_db(conn)

    insert_metadata(conn, "meta_fixture", "fixture_source")
    insert_run(conn, "run_samsung", "005930")
    insert_snapshot(conn, "snap_linked", metadata_id="meta_fixture", run_id="run_samsung", symbol="005930")
    insert_decision(conn, "decision_1", "run_samsung", "005930", horizon=1)

    # On the legacy DB the reported bypasses really are open (001 rerun did not repair them).
    conn.execute("UPDATE ksf_decisions SET feature_snapshot_sha256='tampered-1' WHERE decision_id='decision_1'")
    conn.execute("UPDATE ksf_source_snapshots SET normalized_payload_sha256='deadbeef-1' WHERE snapshot_id='snap_linked'")

    apply_upgrade(conn)  # only the 002 upgrade migration, as on an existing production DB

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE ksf_decisions SET feature_snapshot_sha256='tampered-2' WHERE decision_id='decision_1'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE ksf_source_snapshots SET normalized_payload_sha256='deadbeef-2' WHERE snapshot_id='snap_linked'"
        )

    trigger_sql = {
        name: sql
        for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
    }
    assert "never fires" not in trigger_sql["trg_ksf_decisions_append_only"]
    assert "append-only" in trigger_sql["trg_ksf_decisions_append_only"]
    assert "never fires" not in trigger_sql["trg_ksf_source_snapshots_provenance_immutable"]
    assert "immutable" in trigger_sql["trg_ksf_source_snapshots_provenance_immutable"]

    # Legitimate lifecycle updates keep working on the upgraded DB.
    conn.execute("UPDATE ksf_runs SET run_status='SCORING_DONE' WHERE run_id='run_samsung'")
    conn.execute("UPDATE ksf_source_snapshots SET source_status='STALE' WHERE snapshot_id='snap_linked'")

    apply_upgrade(conn)  # idempotent reapply
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute(
        "SELECT version, COUNT(*) FROM ksf_schema_versions GROUP BY version ORDER BY version"
    ).fetchall() == [(1, 1), (2, 1)]


def test_upgraded_legacy_db_matches_fresh_db_trigger_set():
    fresh = sqlite3.connect(":memory:")
    apply_migration(fresh)
    apply_upgrade(fresh)

    legacy = sqlite3.connect(":memory:")
    apply_migration(legacy)
    simulate_pre_remediation_db(legacy)
    apply_upgrade(legacy)

    def triggers(conn: sqlite3.Connection) -> dict[str, str]:
        return {
            name: " ".join(sql.split())
            for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
        }

    assert triggers(legacy) == triggers(fresh)


def test_snapshot_timestamps_use_absolute_chronology_and_reject_malformed(ledger: sqlite3.Connection):
    insert_metadata(ledger, "meta_fixture", "fixture_source")

    # External source offsets remain valid when their absolute instant is before cutoff.
    insert_snapshot(ledger, "snap_external_tz", metadata_id="meta_fixture")

    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(
            ledger,
            "snap_source_future",
            metadata_id="meta_fixture",
            source_as_of="2026-08-01T07:00:00+00:00",
        )
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(
            ledger,
            "snap_ingested_future",
            metadata_id="meta_fixture",
            ingested_at="2026-08-01T16:00:00+09:00",
        )
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(ledger, "snap_bad_source", metadata_id="meta_fixture", source_as_of="bad")
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(ledger, "snap_bad_ingested", metadata_id="meta_fixture", ingested_at="bad")
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(ledger, "snap_non_kst_ingested", metadata_id="meta_fixture", ingested_at="2026-08-01T06:58:30+00:00")
