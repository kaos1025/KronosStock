import sqlite3

import pytest

from ksf.global_collector import (
    NormalizedFeature,
    SourceSnapshot,
    collect_global_inputs_for_symbol,
    ensure_run,
    init_ledger_schema,
    insert_feature,
    insert_snapshot,
    insert_source_metadata,
    offline_fixture,
    run_smoke,
)


def memory_conn():
    conn = sqlite3.connect(":memory:")
    init_ledger_schema(conn)
    return conn


def collect_once(conn, fixtures=None, **kwargs):
    alpha_key = kwargs.pop("alpha_vantage_key", None)
    bok_key = kwargs.pop("bok_ecos_key", None)
    return collect_global_inputs_for_symbol(
        conn,
        symbol="005930",
        run_id="fixture-run-005930-20260731",
        trading_date="2026-07-31",
        as_of_kst="2026-07-31T16:10:00+09:00",
        available_data_cutoff="2026-07-31T16:00:00+09:00",
        fixtures=offline_fixture() if fixtures is None else fixtures,
        alpha_vantage_key=alpha_key,
        bok_ecos_key=bok_key,
        **kwargs,
    )


def test_fixture_collector_writes_normalized_source_and_features():
    conn = memory_conn()
    result = collect_once(conn)

    assert result.snapshots_inserted == 7
    assert result.features_inserted >= 17
    assert result.source_metadata_inserted == 6
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute(
        """
        SELECT COUNT(*) FROM ksf_source_snapshots s
        JOIN ksf_collected_source_metadata m
          ON m.metadata_id = s.source_metadata_id AND m.source_name = s.source_name
        """
    ).fetchone()[0] == 7

    memory_row = conn.execute(
        """
        SELECT source_status, quality_grade, snapshot_metadata_json, missing_reason
        FROM ksf_source_snapshots
        WHERE source_name='trendforce_dramexchange_memory_license_review'
        """
    ).fetchone()
    assert memory_row[0] == "BLOCKED_REVIEW"
    assert memory_row[1] == "BLOCKED"
    assert '"raw_price_stored":false' in memory_row[2]
    assert "not approved" in memory_row[3]

    soxx_status = conn.execute(
        """
        SELECT feature_status
        FROM ksf_normalized_features
        WHERE feature_name='soxx_adjusted_close'
        """
    ).fetchone()[0]
    assert soxx_status == "STALE"

    usdkrw = conn.execute(
        """
        SELECT s.source_name, f.value_num, s.quality_grade
        FROM ksf_normalized_features f
        JOIN ksf_source_snapshots s ON s.snapshot_id=f.source_snapshot_id
        WHERE f.feature_name='usdkrw'
        """
    ).fetchone()
    assert usdkrw == ("fred_dexkous", 1382.50, "B")
    assert result.fallback_used == {"bok_ecos_usdkrw": "fred_dexkous"}


def test_source_rows_after_cutoff_are_excluded_by_market_timezone():
    conn = memory_conn()
    collect_once(conn)

    mu_close = conn.execute("SELECT value_num FROM ksf_normalized_features WHERE feature_name='mu_adjusted_close'").fetchone()[0]
    usdkrw = conn.execute("SELECT value_num FROM ksf_normalized_features WHERE feature_name='usdkrw'").fetchone()[0]

    # 2026-07-31 US close/FRED rows are not available at 2026-07-31 16:00 KST.
    assert mu_close == 100.0
    assert usdkrw == 1382.50


def test_bok_ecos_primary_path_is_used_when_key_and_payload_exist():
    conn = memory_conn()
    fixtures = offline_fixture()
    fixtures["bok_ecos_usdkrw"] = {
        "StatisticSearch": {
            "row": [
                {"TIME": "2026-07-30", "DATA_VALUE": "1377.20"},
                {"TIME": "2026-07-31", "DATA_VALUE": "1375.10"},
            ]
        }
    }
    result = collect_once(conn, fixtures=fixtures, bok_ecos_key="fixture-key")

    row = conn.execute(
        """
        SELECT s.source_name, f.value_num, s.quality_grade
        FROM ksf_normalized_features f
        JOIN ksf_source_snapshots s ON s.snapshot_id=f.source_snapshot_id
        WHERE f.feature_name='usdkrw'
        """
    ).fetchone()
    assert row == ("bok_ecos_usdkrw", 1375.10, "A")
    assert result.fallback_used == {}


def test_duplicate_load_is_idempotent():
    conn = memory_conn()
    first = collect_once(conn)
    immutable_before = conn.execute(
        """SELECT feature_id, run_id, symbol, source_snapshot_id, feature_group,
                  feature_name, feature_version, value_num, value_text, value_json,
                  source_as_of, ingested_at_kst, available_data_cutoff, created_at_kst
           FROM ksf_normalized_features ORDER BY feature_id"""
    ).fetchall()
    second = collect_once(conn)

    assert first.snapshots_inserted > 0
    assert first.features_inserted > 0
    assert second.snapshots_inserted == 0
    assert second.features_inserted == 0
    # source metadata is shared by capture date and also deduped.
    assert second.source_metadata_inserted == 0

    assert conn.execute("SELECT COUNT(*) FROM ksf_source_snapshots").fetchone()[0] == 7
    assert conn.execute("SELECT COUNT(*) FROM ksf_normalized_features").fetchone()[0] == first.features_inserted
    assert conn.execute(
        """SELECT feature_id, run_id, symbol, source_snapshot_id, feature_group,
                  feature_name, feature_version, value_num, value_text, value_json,
                  source_as_of, ingested_at_kst, available_data_cutoff, created_at_kst
           FROM ksf_normalized_features ORDER BY feature_id"""
    ).fetchall() == immutable_before


def test_run_check_and_foreign_key_violations_are_not_treated_as_duplicates():
    conn = memory_conn()

    with pytest.raises(sqlite3.IntegrityError):
        ensure_run(
            conn,
            run_id="future-cutoff",
            symbol="005930",
            trading_date="2026-07-31",
            as_of_kst="2026-07-31T16:00:00+09:00",
            available_data_cutoff="2026-07-31T16:01:00+09:00",
        )
    with pytest.raises(sqlite3.IntegrityError):
        ensure_run(
            conn,
            run_id="unknown-symbol",
            symbol="999999",
            trading_date="2026-07-31",
            as_of_kst="2026-07-31T16:00:00+09:00",
            available_data_cutoff="2026-07-31T16:00:00+09:00",
        )


def test_snapshot_and_feature_time_checks_are_not_treated_as_duplicates():
    conn = memory_conn()
    ensure_run(
        conn,
        run_id="time-check-run",
        symbol="005930",
        trading_date="2026-07-31",
        as_of_kst="2026-07-31T16:10:00+09:00",
        available_data_cutoff="2026-07-31T16:00:00+09:00",
    )
    insert_source_metadata(conn, [{
        "metadata_id": "time-check-meta",
        "source_name": "time-check-source",
        "capture_date": "2026-07-31",
        "quality_grade": "A",
        "raw_storage_policy": "METADATA_ONLY",
    }])

    malformed = SourceSnapshot(
        "time-check-run", "005930", "time-check-source", "global_price", "CLOSE_CONFIRMED",
        "not-a-date", "2026-07-31T16:00:00+09:00", "2026-07-31T16:00:00+09:00", "A", 0,
    )
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(conn, malformed)

    future = SourceSnapshot(
        "time-check-run", "005930", "time-check-source", "global_price", "CLOSE_CONFIRMED",
        "2026-07-31T16:01:00+09:00", "2026-07-31T16:00:00+09:00",
        "2026-07-31T16:00:00+09:00", "A", 0,
    )
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(conn, future)

    feature = NormalizedFeature(
        run_id="time-check-run", symbol="005930", source_snapshot_id=None,
        feature_group="test", feature_name="future-ingestion", feature_version="v1",
        feature_status="READY", source_as_of="2026-07-31T15:59:00+09:00",
        ingested_at_kst="2026-07-31T16:01:00+09:00",
        available_data_cutoff="2026-07-31T16:00:00+09:00", value_num=1.0,
    )
    with pytest.raises(sqlite3.IntegrityError):
        insert_feature(conn, feature)


def test_writer_trigger_violation_is_not_treated_as_idempotent_zero():
    conn = memory_conn()
    conn.execute(
        """CREATE TRIGGER reject_blocked_metadata BEFORE INSERT ON ksf_collected_source_metadata
           WHEN NEW.quality_grade = 'BLOCKED' BEGIN SELECT RAISE(ABORT, 'blocked by test trigger'); END"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="blocked by test trigger"):
        insert_source_metadata(conn, [{
            "metadata_id": "trigger-rejected-meta",
            "source_name": "trigger-source",
            "capture_date": "2026-07-31",
            "quality_grade": "BLOCKED",
            "raw_storage_policy": "METADATA_ONLY",
        }])


def test_missing_alpha_key_does_not_call_network_and_marks_optional_missing():
    conn = memory_conn()
    calls = []

    def no_network(url, headers=None):
        calls.append(url)
        raise AssertionError("network should not be called for missing Alpha Vantage key when fixtures omit Alpha data")

    fixtures = offline_fixture()
    fixtures = {k: v for k, v in fixtures.items() if k != "alpha_vantage"}
    result = collect_once(conn, fixtures=fixtures, http_get=no_network)

    assert result.statuses["alpha_vantage_daily_adjusted"] == "MISSING"
    assert calls == []
    missing = conn.execute(
        """
        SELECT COUNT(*) FROM ksf_normalized_features
        WHERE feature_group='global_semiconductor_price_context'
          AND feature_status='MISSING_OPTIONAL'
          AND missing_reason LIKE 'missing ALPHAVANTAGE_API_KEY%'
        """
    ).fetchone()[0]
    assert missing >= 9


def test_http_failure_is_captured_as_missing_instead_of_aborting():
    conn = memory_conn()

    def failing_http(url, headers=None):
        if "fredgraph" in url:
            return 503, "temporarily unavailable"
        raise AssertionError(f"unexpected network call: {url}")

    fixtures = {"alpha_vantage": offline_fixture()["alpha_vantage"], "twse_stock_day_2330": offline_fixture()["twse_stock_day_2330"]}
    result = collect_once(conn, fixtures=fixtures, http_get=failing_http)

    assert result.statuses["fred_dexkous"] == "MISSING"
    row = conn.execute("SELECT feature_status, missing_reason FROM ksf_normalized_features WHERE feature_name='usdkrw'").fetchone()
    assert row[0] == "MISSING_OPTIONAL"
    assert "FRED returned HTTP 503" in row[1]


def test_null_numeric_values_are_missing_optional_instead_of_aborting():
    conn = memory_conn()
    fixtures = offline_fixture()
    fixtures["alpha_vantage"] = {
        "MU": {"Time Series (Daily)": {"2026-07-30": {"4. close": "nan", "5. adjusted close": "nan", "6. volume": "nan"}}},
        "NVDA": fixtures["alpha_vantage"]["NVDA"],
        "SOXX": fixtures["alpha_vantage"]["SOXX"],
    }
    fixtures["twse_stock_day_2330"] = {
        "stat": "OK",
        "data": [["115/07/31", "--", "--", "--", "--", "--", "--", "+0.00", "32,000"]],
    }

    collect_once(conn, fixtures=fixtures)

    rows = conn.execute(
        """
        SELECT feature_name, feature_status, missing_reason
        FROM ksf_normalized_features
        WHERE feature_name IN ('mu_adjusted_close', 'mu_volume', 'tsm_twse_close', 'tsm_twse_volume', 'tsm_twse_trade_value')
        ORDER BY feature_name
        """
    ).fetchall()
    assert {row[0]: row[1] for row in rows} == {
        "mu_adjusted_close": "MISSING_OPTIONAL",
        "mu_volume": "MISSING_OPTIONAL",
        "tsm_twse_close": "MISSING_OPTIONAL",
        "tsm_twse_trade_value": "MISSING_OPTIONAL",
        "tsm_twse_volume": "MISSING_OPTIONAL",
    }
    assert all("unavailable" in row[2] for row in rows)


def test_no_secret_offline_smoke_uses_temp_sqlite():
    smoke = run_smoke(live_readonly=False)
    assert smoke["integrity_check"] == "ok"
    assert smoke["counts"]["ksf_source_snapshots"] == 7
    assert smoke["live_readonly"] is False
