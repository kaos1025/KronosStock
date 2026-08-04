from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ksf.performance_settlement import PriceBar, SettlementConfig, SettlementEngine


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "001_k_semiconductor_flow_desk_permanent_ledgers.sql"


@pytest.fixture
def ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(MIGRATION.read_text(encoding="utf-8"))
    conn.execute(
        """INSERT INTO ksf_runs
           (run_id, symbol, trading_date, run_status, as_of_kst, available_data_cutoff)
           VALUES ('run1','005930','2026-05-01','READY',
                   '2026-05-01T16:00:00+09:00','2026-05-01T16:00:00+09:00')"""
    )
    for horizon in (1, 5, 20):
        conn.execute(
            """INSERT INTO ksf_decisions
               (decision_id,run_id,symbol,horizon_days,as_of_kst,available_data_cutoff,
                deterministic_score,score_label,user_opinion,scoring_ruleset_version,
                feature_snapshot_sha256)
               VALUES (?,?,?,?,?,?,60,'positive_watch','POSITIVE','v1','hash')""",
            (f"decision{horizon}", "run1", "005930", horizon,
             "2026-05-01T16:00:00+09:00", "2026-05-01T16:00:00+09:00"),
        )
    return conn


SESSIONS = [
    "2026-05-01", "2026-05-04", "2026-05-06", "2026-05-07", "2026-05-08",
    "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15",
    "2026-05-18", "2026-05-19", "2026-05-20", "2026-05-21", "2026-05-22",
    "2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29",
    "2026-06-01",
]


def bar(symbol: str, date: str, close: float, available: str | None = None) -> PriceBar:
    timestamp = available or f"{date}T16:00:00+09:00"
    return PriceBar(symbol, date, close, timestamp, timestamp, timestamp)


def full_bars(*, future_t1: bool = False) -> list[PriceBar]:
    prices = {"2026-05-01": 100, "2026-05-04": 110, "2026-05-11": 105, "2026-06-01": 120}
    bars: list[PriceBar] = []
    for date, close in prices.items():
        available = "2026-05-05T16:00:00+09:00" if future_t1 and date == "2026-05-04" else None
        bars.append(bar("005930", date, close, available))
    for symbol, closes in {
        "KOSPI": {"2026-05-01": 200, "2026-05-04": 210, "2026-05-11": 204, "2026-06-01": 220},
        "SEMICON": {"2026-05-01": 300, "2026-05-04": 330, "2026-05-11": 315, "2026-06-01": 330},
    }.items():
        bars.extend(bar(symbol, date, close) for date, close in closes.items())
    # Holding-window lows for MDD (T close through target close).
    bars.extend([bar("005930", "2026-05-06", 88), bar("005930", "2026-05-07", 95), bar("005930", "2026-05-08", 104)])
    return bars


def test_holiday_calendar_historical_metrics_and_assumptions(ledger: sqlite3.Connection):
    result = SettlementEngine(ledger, SettlementConfig(turnover=1.0, transaction_cost_bps=15)).settle_due_decisions(
        SESSIONS, full_bars(), "2026-06-01T18:00:00+09:00"
    )
    assert result.settled == 3
    rows = {r["horizon_days"]: r for r in ledger.execute("SELECT * FROM ksf_performance_settlements")}
    assert rows[1]["target_trade_date"] == "2026-05-04"  # weekend skipped
    assert rows[5]["target_trade_date"] == "2026-05-11"  # Children's Day holiday skipped
    assert rows[20]["target_trade_date"] == "2026-06-01"
    assert rows[1]["return_bps"] == pytest.approx(1000)
    assert rows[1]["kospi_excess_return_bps"] == pytest.approx(500)
    assert rows[1]["industry_excess_return_bps"] == pytest.approx(0)
    assert rows[5]["max_drawdown_bps"] == pytest.approx(-2000)
    assert rows[1]["direction_hit"] == 1
    assert rows[1]["status_direction_hit"] == 1
    assert rows[1]["opinion_direction_hit"] == 1
    assert rows[1]["turnover_assumption"] == 1.0
    assert rows[1]["transaction_cost_bps"] == 15
    assert rows[1]["net_return_bps"] == pytest.approx(985)
    assert rows[1]["due_after_kst"] == "2026-05-04T16:00:00+09:00"
    assert rows[5]["due_after_kst"] == "2026-05-11T16:00:00+09:00"
    assert rows[20]["due_after_kst"] == "2026-06-01T16:00:00+09:00"


def test_target_session_before_close_remains_pending(ledger: sqlite3.Connection):
    result = SettlementEngine(ledger).settle_due_decisions(
        SESSIONS, full_bars(), "2026-05-04T15:59:59+09:00"
    )
    row = ledger.execute(
        "SELECT * FROM ksf_performance_settlements WHERE horizon_days=1"
    ).fetchone()
    assert result.pending == 3
    assert row["settlement_status"] == "PENDING_SETTLEMENT"
    assert row["target_trade_date"] == "2026-05-04"
    assert row["due_after_kst"] == "2026-05-04T16:00:00+09:00"


def test_lookahead_is_rejected_then_settles_when_bar_is_available(ledger: sqlite3.Connection):
    engine = SettlementEngine(ledger)
    engine.settle_due_decisions(SESSIONS, full_bars(future_t1=True), "2026-05-04T18:00:00+09:00")
    row = ledger.execute("SELECT * FROM ksf_performance_settlements WHERE horizon_days=1").fetchone()
    assert row["settlement_status"] == "MISSING_PRICE"
    assert "cutoff" in row["missing_reason"]

    # Later rerun may fill a previously missing row, but settled rows are immutable.
    engine.settle_due_decisions(SESSIONS, full_bars(future_t1=True), "2026-05-05T18:00:00+09:00")
    row = ledger.execute("SELECT * FROM ksf_performance_settlements WHERE horizon_days=1").fetchone()
    assert row["settlement_status"] == "SETTLED"
    assert row["target_close"] == 110


def test_idempotent_rerun_keeps_settled_values_stable(ledger: sqlite3.Connection):
    engine = SettlementEngine(ledger)
    engine.settle_due_decisions(SESSIONS, full_bars(), "2026-06-01T18:00:00+09:00")
    before = [tuple(r) for r in ledger.execute("SELECT * FROM ksf_performance_settlements ORDER BY horizon_days")]
    engine.settle_due_decisions(SESSIONS, full_bars() + [bar("005930", "2026-05-04", 999)], "2026-06-01T18:00:00+09:00")
    after = [tuple(r) for r in ledger.execute("SELECT * FROM ksf_performance_settlements ORDER BY horizon_days")]
    assert after == before
    assert len(after) == 3


def test_missing_price_and_calendar_blocked_are_recorded(ledger: sqlite3.Connection):
    engine = SettlementEngine(ledger)
    engine.settle_due_decisions(SESSIONS[:2], [bar("005930", "2026-05-01", 100)], "2026-06-01T18:00:00+09:00")
    rows = {r["horizon_days"]: r for r in ledger.execute("SELECT * FROM ksf_performance_settlements")}
    assert rows[1]["settlement_status"] == "MISSING_PRICE"
    assert rows[1]["missing_reason"]
    assert rows[5]["settlement_status"] == "CALENDAR_BLOCKED"
    assert rows[5]["target_trade_date"] is None
    assert rows[5]["missing_reason"]


def test_negative_state_and_opinion_count_as_win_on_negative_return(ledger: sqlite3.Connection):
    # Decisions are append-only in the ledger: rebuild the fixture decision instead of mutating it.
    ledger.execute("DELETE FROM ksf_decisions WHERE horizon_days=1")
    ledger.execute(
        """INSERT INTO ksf_decisions
           (decision_id,run_id,symbol,horizon_days,as_of_kst,available_data_cutoff,
            deterministic_score,score_label,user_opinion,scoring_ruleset_version,
            feature_snapshot_sha256)
           VALUES ('decision1','run1','005930',1,'2026-05-01T16:00:00+09:00',
                   '2026-05-01T16:00:00+09:00',60,'risk_watch','CAUTION','v1','hash')"""
    )
    bars = [bar("005930", "2026-05-01", 100), bar("005930", "2026-05-04", 90),
            bar("KOSPI", "2026-05-01", 100), bar("KOSPI", "2026-05-04", 100),
            bar("SEMICON", "2026-05-01", 100), bar("SEMICON", "2026-05-04", 100)]
    SettlementEngine(ledger).settle_due_decisions(SESSIONS, bars, "2026-05-04T18:00:00+09:00")
    row = ledger.execute("SELECT * FROM ksf_performance_settlements WHERE horizon_days=1").fetchone()
    assert row["status_direction_hit"] == 1
    assert row["opinion_direction_hit"] == 1


def test_missing_benchmark_bars_do_not_block_settlement(ledger: sqlite3.Connection):
    bars = [bar("005930", "2026-05-01", 100), bar("005930", "2026-05-04", 110)]
    result = SettlementEngine(ledger).settle_due_decisions(
        SESSIONS, bars, "2026-05-04T18:00:00+09:00"
    )
    row = ledger.execute(
        "SELECT * FROM ksf_performance_settlements WHERE horizon_days=1"
    ).fetchone()
    assert result.settled == 1
    assert row["settlement_status"] == "SETTLED"
    assert row["benchmark_return_bps"] is None
    assert row["kospi_return_bps"] is None
    assert row["kospi_excess_return_bps"] is None
    assert row["industry_return_bps"] is None
    assert row["industry_excess_return_bps"] is None
    assert row["due_after_kst"] == "2026-05-04T16:00:00+09:00"
