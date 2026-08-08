import json
import re
import sqlite3
import urllib.parse
from pathlib import Path

import pytest

from ksf.global_collector import (
    ALPHA_VANTAGE_URL,
    NormalizedFeature,
    RequestPacer,
    SourceSnapshot,
    collect_global_inputs,
    collect_global_inputs_for_symbol,
    ensure_run,
    init_ledger_schema,
    insert_feature,
    insert_snapshot,
    insert_source_metadata,
    offline_fixture,
    run_smoke,
)

ROOT = Path(__file__).resolve().parents[1]
SECRET_ALPHA_KEY = "SECRET-ALPHA-KEY-123"


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


# --- Task 1: Alpha Vantage readiness and safe collection ---


def fixtures_without_alpha():
    fixtures = offline_fixture()
    fixtures.pop("alpha_vantage")
    return fixtures


def live_alpha_http(calls):
    """Offline stand-in for the documented read-only Alpha Vantage endpoint."""
    payloads = offline_fixture()["alpha_vantage"]

    def _get(url, headers=None):
        calls.append(url)
        parts = urllib.parse.urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ALPHA_VANTAGE_URL
        query = dict(urllib.parse.parse_qsl(parts.query))
        assert query["function"] == "TIME_SERIES_DAILY_ADJUSTED"
        assert query["outputsize"] == "compact"
        assert query["apikey"] == SECRET_ALPHA_KEY
        return 200, json.dumps(payloads[query["symbol"]])

    return _get


def db_dump(conn):
    return "\n".join(conn.iterdump())


def test_configured_key_uses_documented_readonly_path_without_leaking_secret(capsys):
    conn = memory_conn()
    calls = []
    result = collect_once(
        conn,
        fixtures=fixtures_without_alpha(),
        http_get=live_alpha_http(calls),
        alpha_vantage_key=SECRET_ALPHA_KEY,
    )

    requested = sorted(
        dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["symbol"] for url in calls
    )
    assert requested == ["MU", "NVDA", "SOXX"]
    ready = dict(
        conn.execute(
            """SELECT feature_name, value_num FROM ksf_normalized_features
               WHERE feature_name IN ('mu_adjusted_close','nvda_adjusted_close','soxx_adjusted_close')"""
        ).fetchall()
    )
    # 2026-07-31 US rows are unavailable at the 16:00 KST cutoff → latest usable rows.
    assert ready == {"mu_adjusted_close": 100.0, "nvda_adjusted_close": 175.0, "soxx_adjusted_close": 250.0}

    dump = db_dump(conn)
    captured = capsys.readouterr()
    assert SECRET_ALPHA_KEY not in dump
    assert SECRET_ALPHA_KEY not in captured.out + captured.err
    assert SECRET_ALPHA_KEY not in repr(result)
    # Only the documented placeholder template may mention apikey — never a real value.
    assert "apikey=" not in dump.replace("apikey=$ALPHAVANTAGE_API_KEY", "")


def test_vendor_note_and_information_become_rate_limited_without_raw_text_or_zero():
    conn = memory_conn()
    fixtures = offline_fixture()
    fixtures["alpha_vantage"] = {
        "MU": {"Note": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."},
        "NVDA": {"Information": "This is a premium endpoint notice."},
        "SOXX": fixtures["alpha_vantage"]["SOXX"],
    }
    collect_once(conn, fixtures=fixtures)

    rows = conn.execute(
        """SELECT source_status, missing_reason, snapshot_metadata_json FROM ksf_source_snapshots
           WHERE source_name='alpha_vantage_daily_adjusted'
             AND snapshot_metadata_json LIKE '%VENDOR_RATE_LIMIT_NOTICE%'"""
    ).fetchall()
    assert len(rows) == 2
    for status, reason, metadata in rows:
        assert status == "MISSING"
        assert "rate" in reason.lower()
        assert '"quality_label":"RATE_LIMITED"' in metadata

    features = conn.execute(
        """SELECT feature_name, feature_status, value_num FROM ksf_normalized_features
           WHERE feature_name LIKE 'mu_%' OR feature_name LIKE 'nvda_%'"""
    ).fetchall()
    assert len(features) == 6
    assert all(status == "MISSING_OPTIONAL" and value is None for _, status, value in features)

    dump = db_dump(conn)
    assert "25 requests per day" not in dump
    assert "premium endpoint" not in dump


def test_vendor_error_message_fails_closed_without_persisting_raw_message():
    conn = memory_conn()
    fixtures = offline_fixture()
    fixtures["alpha_vantage"]["MU"] = {"Error Message": "Invalid API call. SECRET-ECHO-VALUE"}
    collect_once(conn, fixtures=fixtures)

    row = conn.execute(
        """SELECT source_status, missing_reason FROM ksf_source_snapshots
           WHERE source_name='alpha_vantage_daily_adjusted'
             AND snapshot_metadata_json LIKE '%VENDOR_ERROR_MESSAGE%'"""
    ).fetchone()
    assert row is not None
    assert row[0] == "MISSING"
    assert "not persisted" in row[1]

    mu = conn.execute(
        "SELECT feature_status, value_num FROM ksf_normalized_features WHERE feature_name LIKE 'mu_%'"
    ).fetchall()
    assert len(mu) == 3
    assert all(status == "MISSING_OPTIONAL" and value is None for status, value in mu)
    assert "SECRET-ECHO-VALUE" not in db_dump(conn)


def test_http_429_is_explicit_rate_limited_for_all_peers():
    conn = memory_conn()

    def throttled(url, headers=None):
        return 429, "Too Many Requests"

    result = collect_once(
        conn,
        fixtures=fixtures_without_alpha(),
        http_get=throttled,
        alpha_vantage_key=SECRET_ALPHA_KEY,
    )

    assert result.statuses["alpha_vantage_daily_adjusted"] == "MISSING"
    count = conn.execute(
        "SELECT COUNT(*) FROM ksf_source_snapshots WHERE snapshot_metadata_json LIKE '%HTTP_429_RATE_LIMIT%'"
    ).fetchone()[0]
    assert count == 3
    features = conn.execute(
        """SELECT feature_status, value_num FROM ksf_normalized_features
           WHERE feature_name LIKE 'mu_%' OR feature_name LIKE 'nvda_%' OR feature_name LIKE 'soxx_%'"""
    ).fetchall()
    assert len(features) == 9
    assert all(status == "MISSING_OPTIONAL" and value is None for status, value in features)
    assert SECRET_ALPHA_KEY not in db_dump(conn)


def test_live_alpha_requests_are_paced_with_bounded_fixed_interval():
    conn = memory_conn()
    sleeps = []
    calls = []
    collect_once(
        conn,
        fixtures=fixtures_without_alpha(),
        http_get=live_alpha_http(calls),
        alpha_vantage_key=SECRET_ALPHA_KEY,
        pacer=RequestPacer(sleep=sleeps.append),
    )

    assert len(calls) == 3
    # First request is immediate; every following live request waits one fixed bounded interval.
    assert len(sleeps) == 2
    assert len(set(sleeps)) == 1
    assert all(0 < interval <= 60 for interval in sleeps)


def test_collect_global_inputs_fetches_each_peer_once_and_reuses_for_all_symbols(monkeypatch):
    conn = memory_conn()
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", SECRET_ALPHA_KEY)
    monkeypatch.delenv("BOK_ECOS_KEY", raising=False)
    sleeps = []
    calls = []

    results = collect_global_inputs(
        conn,
        symbols=("005930", "000660"),
        trading_date="2026-07-31",
        as_of_kst="2026-07-31T16:10:00+09:00",
        available_data_cutoff="2026-07-31T16:00:00+09:00",
        fixtures=fixtures_without_alpha(),
        http_get=live_alpha_http(calls),
        pacer=RequestPacer(sleep=sleeps.append),
    )

    assert set(results) == {"005930", "000660"}
    # Alpha peers are global snapshots: one fetch per peer per run, reused across symbols.
    requested = sorted(
        dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["symbol"] for url in calls
    )
    assert requested == ["MU", "NVDA", "SOXX"]
    assert len(sleeps) == 2
    assert all(0 < interval <= 60 for interval in sleeps)

    # Both domestic symbols keep their own full feature lineage from the shared snapshots.
    # (SOXX fixture row is 6 days old → labeled STALE by design; the value is still recorded.)
    rows = conn.execute(
        """SELECT symbol, feature_name, feature_status, value_num FROM ksf_normalized_features
           WHERE feature_name IN ('mu_adjusted_close','nvda_adjusted_close','soxx_adjusted_close')"""
    ).fetchall()
    assert len(rows) == 6
    assert {status for _, _, status, _ in rows} == {"READY", "STALE"}
    for symbol in ("005930", "000660"):
        values = {name: value for sym, name, _, value in rows if sym == symbol}
        assert values == {"mu_adjusted_close": 100.0, "nvda_adjusted_close": 175.0, "soxx_adjusted_close": 250.0}
    assert SECRET_ALPHA_KEY not in db_dump(conn)


def test_offline_fixture_and_missing_key_paths_never_sleep():
    sleeps = []
    collect_once(memory_conn(), pacer=RequestPacer(sleep=sleeps.append))
    assert sleeps == []

    def no_network(url, headers=None):
        raise AssertionError("network must not be called")

    collect_once(
        memory_conn(),
        fixtures=fixtures_without_alpha(),
        http_get=no_network,
        pacer=RequestPacer(sleep=sleeps.append),
    )
    assert sleeps == []


def test_future_dated_only_rows_fail_closed_as_explicit_missing():
    conn = memory_conn()
    fixtures = offline_fixture()
    fixtures["alpha_vantage"]["MU"] = {
        "Time Series (Daily)": {
            "2026-08-03": {"4. close": "111.00", "5. adjusted close": "111.00", "6. volume": "1"}
        }
    }
    collect_once(conn, fixtures=fixtures)

    row = conn.execute(
        """SELECT source_status, missing_reason FROM ksf_source_snapshots
           WHERE source_name='alpha_vantage_daily_adjusted'
             AND snapshot_metadata_json LIKE '%NO_OBSERVATION_AT_OR_BEFORE_CUTOFF%'"""
    ).fetchone()
    assert row is not None
    assert row[0] == "MISSING"
    assert "cutoff" in row[1]

    mu = conn.execute(
        "SELECT feature_status, value_num FROM ksf_normalized_features WHERE feature_name LIKE 'mu_%'"
    ).fetchall()
    assert len(mu) == 3
    assert all(status == "MISSING_OPTIONAL" and value is None for status, value in mu)


def test_generic_http_or_schema_failure_yields_all_three_peer_features_missing():
    conn = memory_conn()

    def broken(url, headers=None):
        return 200, "this is not json {"

    collect_once(
        conn,
        fixtures=fixtures_without_alpha(),
        http_get=broken,
        alpha_vantage_key=SECRET_ALPHA_KEY,
    )

    for peer in ("mu", "nvda", "soxx"):
        rows = conn.execute(
            "SELECT feature_name, feature_status, value_num FROM ksf_normalized_features WHERE feature_name LIKE ?",
            (f"{peer}_%",),
        ).fetchall()
        assert sorted(name for name, _, _ in rows) == [
            f"{peer}_adjusted_close",
            f"{peer}_one_day_return_bps",
            f"{peer}_volume",
        ]
        assert all(status == "MISSING_OPTIONAL" and value is None for _, status, value in rows)
    assert SECRET_ALPHA_KEY not in db_dump(conn)


def test_rate_limited_persistence_is_idempotent():
    conn = memory_conn()
    fixtures = offline_fixture()
    fixtures["alpha_vantage"]["MU"] = {"Note": "throttled"}
    first = collect_once(conn, fixtures=fixtures)
    second = collect_once(conn, fixtures=fixtures)

    assert first.snapshots_inserted == 7
    assert first.features_inserted > 0
    assert second.snapshots_inserted == 0
    assert second.features_inserted == 0


def test_env_example_documents_alpha_vantage_production_config_without_value():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "Alpha Vantage" in text
    # Documented as production config, never with an embedded secret value.
    assert re.search(r"^ALPHAVANTAGE_API_KEY=$", text, re.MULTILINE)


def test_settings_expose_alpha_vantage_readiness_without_requiring_secret(monkeypatch):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    from common.config import Settings

    assert Settings(_env_file=None).alphavantage_configured is False
    assert Settings(_env_file=None, alphavantage_api_key="k").alphavantage_configured is True
