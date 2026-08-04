from __future__ import annotations

import sqlite3

import pytest

from ksf.feature_engine import (
    CANONICAL_RUN_STATES,
    FeatureEngine,
    FeatureEngineConfig,
    validate_run_state,
)
from ksf.global_collector import init_ledger_schema, stable_id


CUTOFF = "2026-07-31T16:00:00+09:00"
AS_OF = "2026-07-31T16:10:00+09:00"


@pytest.fixture
def ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_ledger_schema(conn)
    return conn


def add_feature(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    name: str,
    group: str,
    value: float | None = 1.0,
    status: str = "READY",
    source_as_of: str = "2026-07-31T15:30:00+09:00",
    ingested_at: str = "2026-07-31T15:45:00+09:00",
    missing_reason: str | None = None,
    ledger_cutoff: str = CUTOFF,
) -> None:
    run_id = stable_id("input_run", {"symbol": symbol, "cutoff": ledger_cutoff})
    conn.execute(
        """INSERT OR IGNORE INTO ksf_runs
           (run_id, symbol, trading_date, run_status, as_of_kst,
            available_data_cutoff, scoring_ruleset_version)
           VALUES (?, ?, '2026-07-31', 'READY', ?, ?, 'fixture-v1')""",
        (run_id, symbol, max(AS_OF, ledger_cutoff), ledger_cutoff),
    )
    feature_id = stable_id(
        "input_feat", {"run_id": run_id, "name": name, "group": group, "source_as_of": source_as_of}
    )
    conn.execute(
        """INSERT INTO ksf_normalized_features
           (feature_id, run_id, symbol, feature_group, feature_name,
            feature_version, feature_status, value_num, source_as_of,
            ingested_at_kst, available_data_cutoff, contribution_cap_bps,
            missing_reason)
           VALUES (?, ?, ?, ?, ?, 'v1', ?, ?, ?, ?, ?, 10000, ?)""",
        (
            feature_id,
            run_id,
            symbol,
            group,
            name,
            status,
            value,
            source_as_of,
            ingested_at,
            ledger_cutoff,
            missing_reason,
        ),
    )


def add_complete_inputs(conn: sqlite3.Connection, symbol: str) -> None:
    for name, value in (
        ("close", 70000),
        ("volume", 10_000_000),
        ("one_day_return_bps", 125),
    ):
        add_feature(conn, symbol=symbol, name=name, group="domestic_price", value=value)
    for name, value in (
        ("foreigner_net_buy_quantity", 1000),
        ("institution_total_net_buy_quantity", 500),
    ):
        add_feature(conn, symbol=symbol, name=name, group="domestic_investor_flow", value=value)
    for name, value in (
        ("mu_one_day_return_bps", 900),
        ("nvda_one_day_return_bps", 800),
        ("soxx_one_day_return_bps", 700),
        ("tsm_twse_close", 950),
        ("usdkrw", 1380),
    ):
        add_feature(conn, symbol=symbol, name=name, group="global_semiconductor_price_context", value=value)
    add_feature(conn, symbol=symbol, name="earnings_event_days", group="event", value=3)
    add_feature(
        conn,
        symbol=symbol,
        name="hbm_supply_tightness",
        group="global_memory_price_context",
        value=None,
        status="LICENSE_BLOCKED",
        missing_reason="license review required",
    )


def test_builds_independent_outputs_with_all_groups_and_no_comparison_fields(ledger):
    for symbol in ("005930", "000660"):
        add_complete_inputs(ledger, symbol)
    add_feature(ledger, symbol="000660", name="foreigner_net_buy_quantity_extra", group="domestic_investor_flow", value=-999)

    outputs = FeatureEngine(ledger).build_many(
        ("005930", "000660"), available_data_cutoff=CUTOFF, as_of_kst=AS_OF
    )

    assert tuple(outputs) == ("005930", "000660")
    assert outputs["005930"].symbol == "005930"
    assert outputs["005930"].state == "READY"
    assert set(outputs["005930"].groups) == {
        "domestic_investor_flow",
        "price_volume",
        "global_memory_price",
        "ai_hbm_demand",
        "semiconductor_regime",
        "fx_market",
        "event",
    }
    serialized = outputs["005930"].to_dict()
    assert not ({"rank", "relative_rank", "peer_score", "pair_signal"} & serialized.keys())
    assert "000660" not in str(serialized)


def test_correlated_global_signals_are_deduplicated_and_group_capped(ledger):
    add_complete_inputs(ledger, "005930")
    # A corrected/reloaded feature with the same semantic name must not become a
    # second vote.  The later source row wins deterministically.
    add_feature(
        ledger,
        symbol="005930",
        name="mu_one_day_return_bps",
        group="alternate_global_feed",
        value=950,
        source_as_of="2026-07-31T15:40:00+09:00",
        ingested_at="2026-07-31T15:50:00+09:00",
    )

    output = FeatureEngine(ledger).build_symbol("005930", available_data_cutoff=CUTOFF, as_of_kst=AS_OF)

    memory = output.groups["global_memory_price"]
    assert [item.name for item in memory.features].count("mu_one_day_return_bps") == 1
    assert memory.features[0].value == 950
    assert abs(memory.contribution_bps) <= memory.cap_bps == 1500
    assert abs(output.groups["ai_hbm_demand"].contribution_bps) <= 1500
    assert abs(output.groups["semiconductor_regime"].contribution_bps) <= 1500


@pytest.mark.parametrize(
    ("omitted", "expected_state"),
    [
        ("close", "MISSING_REQUIRED_DATA"),
        ("volume", "MISSING_REQUIRED_DATA"),
        ("foreigner_net_buy_quantity", "MISSING_REQUIRED_DATA"),
    ],
)
def test_missing_required_inputs_close_gate(ledger, omitted, expected_state):
    add_complete_inputs(ledger, "005930")
    ledger.execute("DELETE FROM ksf_normalized_features WHERE symbol='005930' AND feature_name=?", (omitted,))

    output = FeatureEngine(ledger).build_symbol("005930", available_data_cutoff=CUTOFF, as_of_kst=AS_OF)

    assert output.state == expected_state
    assert omitted in output.missing_required
    assert output.scoring_allowed is False


def test_stale_required_input_closes_gate_and_future_rows_are_ignored(ledger):
    add_complete_inputs(ledger, "005930")
    ledger.execute(
        "UPDATE ksf_normalized_features SET feature_status='STALE' WHERE feature_name='close'"
    )
    add_feature(
        ledger,
        symbol="005930",
        name="close",
        group="domestic_price",
        value=999999,
        source_as_of="2026-07-31T16:05:00+09:00",
        ingested_at="2026-07-31T16:06:00+09:00",
        ledger_cutoff="2026-07-31T16:06:00+09:00",
    )

    output = FeatureEngine(ledger).build_symbol("005930", available_data_cutoff=CUTOFF, as_of_kst=AS_OF)

    assert output.state == "STALE_DATA"
    assert output.scoring_allowed is False
    close = next(f for f in output.groups["price_volume"].features if f.name == "close")
    assert close.value == 70000


def test_mixed_offset_row_before_equivalent_utc_cutoff_is_included(ledger):
    add_feature(
        ledger,
        symbol="005930",
        name="close",
        group="domestic_price",
        value=70000,
        source_as_of="2026-07-31T15:30:00+09:00",
        ingested_at="2026-07-31T15:45:00+09:00",
        ledger_cutoff="2026-07-31T15:45:00+09:00",
    )

    output = FeatureEngine(ledger).build_symbol(
        "005930",
        available_data_cutoff="2026-07-31T07:00:00Z",
        as_of_kst="2026-07-31T07:10:00Z",
    )

    close = next(feature for feature in output.groups["price_volume"].features if feature.name == "close")
    assert close.value == 70000


def test_mixed_offset_future_row_is_excluded(ledger):
    # Model an existing mixed-offset ledger. Current schema constraints require
    # KST for ingestion/cutoff fields, but the reader must remain correct for
    # legacy/imported rows whose valid ISO timestamps use UTC notation.
    ledger.execute("PRAGMA ignore_check_constraints = ON")
    add_feature(
        ledger,
        symbol="005930",
        name="close",
        group="domestic_price",
        value=70000,
        source_as_of="2026-07-31T06:30:00Z",
        ingested_at="2026-07-31T06:45:00Z",
        ledger_cutoff="2026-07-31T06:45:00Z",
    )
    add_feature(
        ledger,
        symbol="005930",
        name="close",
        group="domestic_price",
        value=999999,
        source_as_of="2026-07-31T07:30:00Z",
        ingested_at="2026-07-31T07:31:00Z",
        ledger_cutoff="2026-07-31T07:31:00Z",
    )

    output = FeatureEngine(ledger).build_symbol(
        "005930",
        available_data_cutoff="2026-07-31T16:00:00+09:00",
        as_of_kst="2026-07-31T16:10:00+09:00",
    )

    close = next(feature for feature in output.groups["price_volume"].features if feature.name == "close")
    assert close.value == 70000


def test_optional_gaps_produce_partial_state_but_license_block_does_not_block_run(ledger):
    add_complete_inputs(ledger, "005930")
    ledger.execute("DELETE FROM ksf_normalized_features WHERE feature_name='usdkrw'")

    output = FeatureEngine(ledger).build_symbol("005930", available_data_cutoff=CUTOFF, as_of_kst=AS_OF)

    assert output.state == "PARTIAL_DATA"
    assert output.scoring_allowed is True
    assert "usdkrw" in output.missing_optional
    assert "hbm_supply_tightness" in output.license_blocked
    assert output.groups["ai_hbm_demand"].contribution_bps == 800
    hbm = next(f for f in output.groups["ai_hbm_demand"].features if f.name == "hbm_supply_tightness")
    assert hbm.status == "LICENSE_BLOCKED"


def test_output_is_byte_reproducible_regardless_of_insert_and_symbol_order(ledger):
    add_complete_inputs(ledger, "000660")
    add_complete_inputs(ledger, "005930")
    engine = FeatureEngine(ledger)

    first = engine.build_many(("005930", "000660"), available_data_cutoff=CUTOFF, as_of_kst=AS_OF)
    second = engine.build_many(("000660", "005930"), available_data_cutoff=CUTOFF, as_of_kst=AS_OF)

    assert first["005930"].stable_json() == second["005930"].stable_json()
    assert first["000660"].stable_json() == second["000660"].stable_json()
    assert first["005930"].output_id == second["005930"].output_id


def test_state_taxonomy_is_canonical_and_rejects_mixed_taxonomies():
    assert CANONICAL_RUN_STATES == frozenset(
        {
            "READY",
            "PARTIAL_DATA",
            "STALE_DATA",
            "MISSING_REQUIRED_DATA",
            "SCORING_DONE",
            "AI_SUMMARY_DONE",
            "BLOCKED_REVIEW",
            "FAILED",
            "ARCHIVED",
        }
    )
    assert validate_run_state("READY") == "READY"
    with pytest.raises(ValueError, match="canonical run state"):
        validate_run_state("MISSING_OPTIONAL")


def test_rejects_unsupported_symbols(ledger):
    with pytest.raises(ValueError, match="unsupported symbol"):
        FeatureEngine(ledger, FeatureEngineConfig()).build_symbol(
            "035420", available_data_cutoff=CUTOFF, as_of_kst=AS_OF
        )
