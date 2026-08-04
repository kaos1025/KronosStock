"""Deterministic, offline post-decision performance settlement.

The caller supplies a KRX trading-session calendar and close-confirmed bars.
No network, broker, order, or external-write capability is used here.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

from ksf.global_collector import KST, parse_cutoff, stable_id


@dataclass(frozen=True)
class PriceBar:
    symbol: str
    trade_date: str
    close: float
    source_as_of: str
    ingested_at_kst: str
    available_data_cutoff: str
    source_snapshot_id: str | None = None
    source_status: str = "CLOSE_CONFIRMED"


@dataclass(frozen=True)
class SettlementConfig:
    kospi_symbol: str = "KOSPI"
    industry_symbol: str = "SEMICON"
    turnover: float = 1.0
    transaction_cost_bps: float = 0.0


@dataclass(frozen=True)
class SettlementResult:
    settled: int = 0
    pending: int = 0
    missing: int = 0
    calendar_blocked: int = 0


def _is_available(bar: PriceBar, cutoff: dt.datetime) -> bool:
    if bar.source_status != "CLOSE_CONFIRMED" or bar.close <= 0:
        return False
    try:
        return all(parse_cutoff(value) <= cutoff for value in (
            bar.source_as_of, bar.ingested_at_kst, bar.available_data_cutoff
        ))
    except (TypeError, ValueError):
        return False


def _return_bps(base: float, target: float) -> float:
    return (target / base - 1.0) * 10_000.0


def _hit(signal: int, return_bps: float) -> int:
    return int((signal > 0 and return_bps > 0) or (signal < 0 and return_bps < 0) or (signal == 0 and return_bps == 0))


def _target_due_kst(target_date: str) -> str:
    return f"{target_date}T16:00:00+09:00"


class SettlementEngine:
    def __init__(self, connection: sqlite3.Connection, config: SettlementConfig | None = None):
        self.connection = connection
        self.config = config or SettlementConfig()

    def settle_due_decisions(
        self,
        trading_sessions: Sequence[str],
        price_bars: Iterable[PriceBar],
        as_of_kst: str,
    ) -> SettlementResult:
        cutoff = parse_cutoff(as_of_kst)
        if cutoff.utcoffset() != KST.utcoffset(None) or not as_of_kst.endswith("+09:00"):
            raise ValueError("as_of_kst must be an offset-aware +09:00 timestamp")
        sessions = tuple(dict.fromkeys(trading_sessions))
        if list(sessions) != sorted(sessions):
            raise ValueError("trading_sessions must be unique and sorted")
        index = {date: pos for pos, date in enumerate(sessions)}
        bars_by_key: dict[tuple[str, str], list[PriceBar]] = {}
        for bar in price_bars:
            bars_by_key.setdefault((bar.symbol, bar.trade_date), []).append(bar)

        counts = {"settled": 0, "pending": 0, "missing": 0, "calendar_blocked": 0}
        decisions = self.connection.execute(
            """SELECT d.*, r.trading_date FROM ksf_decisions d
               JOIN ksf_runs r ON r.run_id=d.run_id ORDER BY d.decision_id"""
        ).fetchall()
        columns = [item[0] for item in self.connection.execute(
            """SELECT d.*, r.trading_date FROM ksf_decisions d
               JOIN ksf_runs r ON r.run_id=d.run_id LIMIT 0""").description]
        for raw in decisions:
            decision = dict(zip(columns, raw))
            existing = self.connection.execute(
                "SELECT settlement_status FROM ksf_performance_settlements WHERE decision_id=? AND horizon_days=?",
                (decision["decision_id"], decision["horizon_days"]),
            ).fetchone()
            if existing and existing[0] == "SETTLED":
                continue
            base_date = decision["trading_date"]
            position = index.get(base_date)
            target_position = None if position is None else position + decision["horizon_days"]
            if target_position is None or target_position >= len(sessions):
                self._upsert_status(decision, base_date, None, "CALENDAR_BLOCKED", as_of_kst, as_of_kst,
                                    "calendar does not contain enough sessions for horizon")
                counts["calendar_blocked"] += 1
                continue
            target_date = sessions[target_position]
            due_after_kst = _target_due_kst(target_date)
            if cutoff < parse_cutoff(due_after_kst):
                self._upsert_status(decision, base_date, target_date, "PENDING_SETTLEMENT",
                                    due_after_kst, as_of_kst, None)
                counts["pending"] += 1
                continue
            base = self._select_bar(bars_by_key, decision["symbol"], base_date, cutoff)
            target = self._select_bar(bars_by_key, decision["symbol"], target_date, cutoff)
            if base is None or target is None:
                reason = "eligible close-confirmed base/target price unavailable at settlement cutoff"
                self._upsert_status(decision, base_date, target_date, "MISSING_PRICE",
                                    due_after_kst, as_of_kst, reason)
                counts["missing"] += 1
                continue
            self._settle(decision, base_date, target_date, base, target, sessions[position:target_position + 1],
                         bars_by_key, cutoff, due_after_kst, as_of_kst)
            counts["settled"] += 1
        self.connection.commit()
        return SettlementResult(**counts)

    @staticmethod
    def _select_bar(bars: dict[tuple[str, str], list[PriceBar]], symbol: str, date: str,
                    cutoff: dt.datetime) -> PriceBar | None:
        eligible = [bar for bar in bars.get((symbol, date), ()) if _is_available(bar, cutoff)]
        return min(eligible, key=lambda bar: (parse_cutoff(bar.available_data_cutoff), bar.close)) if eligible else None

    def _identity(self, decision: dict, target_date: str | None) -> str:
        return stable_id("settle", {"decision_id": decision["decision_id"],
                                    "horizon_days": decision["horizon_days"], "target_trade_date": target_date})

    def _upsert_status(self, decision: dict, base_date: str, target_date: str | None,
                       status: str, due_after: str, cutoff: str, reason: str | None) -> None:
        self.connection.execute(
            """INSERT INTO ksf_performance_settlements
               (settlement_id,decision_id,run_id,symbol,horizon_days,base_trade_date,target_trade_date,
                settlement_status,due_after_kst,missing_reason,settlement_cutoff_kst)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(decision_id,horizon_days) DO UPDATE SET
                 target_trade_date=excluded.target_trade_date, settlement_status=excluded.settlement_status,
                 due_after_kst=excluded.due_after_kst, missing_reason=excluded.missing_reason,
                 settlement_cutoff_kst=excluded.settlement_cutoff_kst""",
            (self._identity(decision, target_date), decision["decision_id"], decision["run_id"], decision["symbol"],
             decision["horizon_days"], base_date, target_date, status, due_after, reason, cutoff),
        )

    def _benchmark(self, bars, symbol, base_date, target_date, cutoff):
        base = self._select_bar(bars, symbol, base_date, cutoff)
        target = self._select_bar(bars, symbol, target_date, cutoff)
        return None if base is None or target is None else _return_bps(base.close, target.close)

    def _settle(self, decision, base_date, target_date, base, target, window, bars, cutoff, due_after, as_of):
        raw_return = _return_bps(base.close, target.close)
        kospi = self._benchmark(bars, self.config.kospi_symbol, base_date, target_date, cutoff)
        industry = self._benchmark(bars, self.config.industry_symbol, base_date, target_date, cutoff)
        closes = [self._select_bar(bars, decision["symbol"], date, cutoff) for date in window]
        values = [bar.close for bar in closes if bar is not None]
        peak = values[0]
        mdd = 0.0
        for value in values:
            peak = max(peak, value)
            mdd = min(mdd, _return_bps(peak, value))
        status_signal = {"positive_watch": 1, "risk_watch": -1}.get(decision["score_label"], 0)
        opinion_signal = ({"POSITIVE": 1, "ACCUMULATION": 1, "CAUTION": -1, "DISTRIBUTION": -1}
                          .get(decision["user_opinion"], 0))
        score_signal = 1 if (decision["deterministic_score"] or 0) > 0 else -1 if (decision["deterministic_score"] or 0) < 0 else 0
        cost = self.config.turnover * self.config.transaction_cost_bps
        self.connection.execute(
            """INSERT INTO ksf_performance_settlements
               (settlement_id,decision_id,run_id,symbol,horizon_days,base_trade_date,target_trade_date,
                settlement_status,base_close,target_close,return_bps,benchmark_return_bps,direction_hit,
                kospi_return_bps,kospi_excess_return_bps,industry_return_bps,industry_excess_return_bps,
                status_direction_hit,opinion_direction_hit,max_drawdown_bps,turnover_assumption,
                transaction_cost_bps,net_return_bps,settlement_source_snapshot_id,due_after_kst,
                settled_at_kst,settlement_cutoff_kst)
               VALUES (?,?,?,?,?,?,?,'SETTLED',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(decision_id,horizon_days) DO UPDATE SET
                target_trade_date=excluded.target_trade_date,settlement_status='SETTLED',base_close=excluded.base_close,
                target_close=excluded.target_close,return_bps=excluded.return_bps,benchmark_return_bps=excluded.benchmark_return_bps,
                direction_hit=excluded.direction_hit,kospi_return_bps=excluded.kospi_return_bps,
                kospi_excess_return_bps=excluded.kospi_excess_return_bps,industry_return_bps=excluded.industry_return_bps,
                industry_excess_return_bps=excluded.industry_excess_return_bps,status_direction_hit=excluded.status_direction_hit,
                opinion_direction_hit=excluded.opinion_direction_hit,max_drawdown_bps=excluded.max_drawdown_bps,
                turnover_assumption=excluded.turnover_assumption,transaction_cost_bps=excluded.transaction_cost_bps,
                net_return_bps=excluded.net_return_bps,settlement_source_snapshot_id=excluded.settlement_source_snapshot_id,
                due_after_kst=excluded.due_after_kst,settled_at_kst=excluded.settled_at_kst,
                settlement_cutoff_kst=excluded.settlement_cutoff_kst,missing_reason=NULL""",
            (self._identity(decision, target_date), decision["decision_id"], decision["run_id"], decision["symbol"],
             decision["horizon_days"], base_date, target_date, base.close, target.close, raw_return, kospi,
             _hit(score_signal, raw_return), kospi, None if kospi is None else raw_return-kospi,
             industry, None if industry is None else raw_return-industry, _hit(status_signal, raw_return),
             _hit(opinion_signal, raw_return), mdd, self.config.turnover, self.config.transaction_cost_bps,
             raw_return-cost, target.source_snapshot_id, due_after, as_of, as_of),
        )
