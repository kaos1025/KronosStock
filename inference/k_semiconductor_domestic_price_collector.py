"""Samsung Electronics and SK hynix domestic KIS price/volume collector.

Scope is intentionally narrow: read-only KIS quotation daily chart rows for
exactly 005930/000660, normalized into the KSF SQLite ledger. No account,
balance, broker, trading, or order capability is imported or called.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from inference.k_semiconductor_investor_flow_collector import (
    KIS_BASE_URL,
    XKRX_SESSION_CLOSE,
    KisInvestorFlowClient,
    XkrxCalendarUnavailableError,
    _completed_session_violation,
    _xkrx_sessions,
)

KST = timezone(timedelta(hours=9))
SUPPORTED_SYMBOLS = ("005930", "000660")
DAILY_ITEMCHART_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
DAILY_ITEMCHART_SOURCE_NAME = "KIS_DAILY_ITEMCHARTPRICE"


class StaleDomesticPriceError(RuntimeError):
    """Latest usable domestic price row is too many XKRX sessions old."""


class DomesticPriceClient(Protocol):
    def fetch_daily_itemchartprice(self, symbol: str, *, start_date: str, end_date: str) -> list[dict[str, str]]: ...


@dataclass(frozen=True)
class DomesticPriceRecord:
    symbol: str
    trade_date: date
    close: float
    volume: int
    source_as_of: str
    source_status: str
    one_day_return_bps: float | None = None


@dataclass(frozen=True)
class DomesticPriceStoreResult:
    inserted_runs: int
    inserted_snapshots: int
    inserted_features: int


class DomesticPriceCollector:
    def __init__(
        self,
        *,
        client: DomesticPriceClient,
        stale_after_sessions: int = 0,
        lookback_days: int = 30,
    ) -> None:
        self.client = client
        self.stale_after_sessions = stale_after_sessions
        self.lookback_days = lookback_days

    def collect_symbol(self, symbol: str, *, available_data_cutoff: str) -> DomesticPriceRecord:
        if symbol not in SUPPORTED_SYMBOLS:
            raise ValueError(f"unsupported symbol: {symbol}")
        cutoff_dt = _parse_kst_timestamp(available_data_cutoff)
        end = cutoff_dt.date()
        start = end - timedelta(days=self.lookback_days)
        rows = self.client.fetch_daily_itemchartprice(
            symbol,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        candidates = [_parse_price_row(row, cutoff_dt, symbol) for row in rows]
        valid = [item for item in candidates if item is not None]
        if not valid:
            raise RuntimeError(f"no valid KIS daily price row for {symbol} before cutoff")
        valid.sort(key=lambda item: item["trade_date"], reverse=True)
        latest = valid[0]
        _validate_completed_xkrx_session(symbol, latest["trade_date"], cutoff_dt, self.stale_after_sessions)
        earlier = next((item for item in valid[1:] if item["trade_date"] < latest["trade_date"]), None)
        one_day_return_bps = None
        if earlier is not None:
            one_day_return_bps = ((latest["close"] / earlier["close"]) - 1.0) * 10000.0
        return DomesticPriceRecord(
            symbol=symbol,
            trade_date=latest["trade_date"],
            close=latest["close"],
            volume=latest["volume"],
            source_as_of=_source_as_of(latest["trade_date"]),
            source_status="INTRADAY_ESTIMATE",
            one_day_return_bps=one_day_return_bps,
        )

    def collect_symbols(self, symbols: Iterable[str] = SUPPORTED_SYMBOLS, *, available_data_cutoff: str) -> list[DomesticPriceRecord]:
        return [self.collect_symbol(symbol, available_data_cutoff=available_data_cutoff) for symbol in symbols]


class DomesticPriceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def store_records(
        self,
        records: Iterable[DomesticPriceRecord],
        *,
        as_of_kst: str,
        available_data_cutoff: str,
    ) -> DomesticPriceStoreResult:
        as_of = _parse_kst_timestamp(as_of_kst)
        _parse_kst_timestamp(available_data_cutoff)
        inserted_runs = inserted_snapshots = inserted_features = 0
        capture_date = as_of.date().isoformat()
        metadata_id = _stable_id("ksf_meta", DAILY_ITEMCHART_SOURCE_NAME, capture_date)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO ksf_collected_source_metadata
                (metadata_id, source_name, endpoint_template, method, source_url,
                 license_scope, rate_limit_policy, user_agent_policy, capture_date,
                 quality_grade, raw_storage_policy, redistribution_policy, notes)
                VALUES (?, ?, ?, 'GET', ?, 'internal_readonly_quotation_analysis',
                        'KIS quotation API pacing applies', 'KronosStock KIS read-only collector',
                        ?, 'B', 'METADATA_ONLY',
                        'no_raw_redistribution_without_license', ?)
                ON CONFLICT(metadata_id) DO NOTHING
                """,
                (
                    metadata_id,
                    DAILY_ITEMCHART_SOURCE_NAME,
                    DAILY_ITEMCHART_ENDPOINT,
                    f"{KIS_BASE_URL}{DAILY_ITEMCHART_ENDPOINT}",
                    capture_date,
                    "KIS daily item chart normalized close/volume only; no raw payload stored",
                ),
            )
            for record in records:
                if record.symbol not in SUPPORTED_SYMBOLS:
                    raise ValueError(f"unsupported symbol: {record.symbol}")
                payload = {
                    "symbol": record.symbol,
                    "trade_date": record.trade_date.isoformat(),
                    "close": record.close,
                    "volume": record.volume,
                    "source_name": DAILY_ITEMCHART_SOURCE_NAME,
                    "source_status": record.source_status,
                    "one_day_return_bps": record.one_day_return_bps,
                }
                payload_hash = _sha256_json(payload)
                run_id = _stable_id(
                    "ksf_run",
                    record.symbol,
                    record.trade_date.isoformat(),
                    available_data_cutoff,
                    "domestic_price_v1",
                )
                before = self.conn.total_changes
                self.conn.execute(
                    """
                    INSERT INTO ksf_runs (
                        run_id, symbol, trading_date, run_status, as_of_kst, available_data_cutoff,
                        scoring_ruleset_version, prompt_template_version, model_policy_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO NOTHING
                    """,
                    (
                        run_id,
                        record.symbol,
                        record.trade_date.isoformat(),
                        "READY",
                        as_of_kst,
                        available_data_cutoff,
                        "domestic_price_collector_v1",
                        "none",
                        "none",
                    ),
                )
                inserted_runs += self.conn.total_changes - before
                snapshot_id = _stable_id("ksf_src", run_id, DAILY_ITEMCHART_SOURCE_NAME, payload_hash)
                before = self.conn.total_changes
                self.conn.execute(
                    """
                    INSERT INTO ksf_source_snapshots (
                        snapshot_id, run_id, symbol, source_metadata_id, source_name, source_kind, source_status,
                        source_as_of, ingested_at_kst, available_data_cutoff, quality_grade,
                        normalized_payload_sha256, snapshot_metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_id) DO NOTHING
                    """,
                    (
                        snapshot_id,
                        run_id,
                        record.symbol,
                        metadata_id,
                        DAILY_ITEMCHART_SOURCE_NAME,
                        "domestic_price",
                        record.source_status,
                        record.source_as_of,
                        as_of_kst,
                        available_data_cutoff,
                        "B",
                        payload_hash,
                        json.dumps(
                            {
                                "normalized_fields": ["close", "volume", "one_day_return_bps"],
                                "raw_storage_policy": "METADATA_ONLY",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
                inserted_snapshots += self.conn.total_changes - before
                feature_specs = [
                    ("close", record.close, "KRW"),
                    ("volume", float(record.volume), "shares"),
                ]
                if record.one_day_return_bps is not None:
                    feature_specs.append(("one_day_return_bps", record.one_day_return_bps, "bps"))
                for feature_name, value_num, unit in feature_specs:
                    feature_payload = {**payload, "feature_name": feature_name, "value_num": value_num, "unit": unit}
                    feature_hash = _sha256_json(feature_payload)
                    feature_version = f"v1:{feature_hash[:12]}"
                    feature_id = _stable_id("ksf_feat", run_id, feature_name, feature_version)
                    before = self.conn.total_changes
                    self.conn.execute(
                        """
                        INSERT INTO ksf_normalized_features (
                            feature_id, run_id, symbol, source_snapshot_id, feature_group, feature_name,
                            feature_version, feature_status, value_num, value_json, unit,
                            source_as_of, ingested_at_kst, available_data_cutoff
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(feature_id) DO NOTHING
                        """,
                        (
                            feature_id,
                            run_id,
                            record.symbol,
                            snapshot_id,
                            "domestic_price",
                            feature_name,
                            feature_version,
                            "READY",
                            float(value_num),
                            json.dumps(feature_payload, ensure_ascii=False, sort_keys=True),
                            unit,
                            record.source_as_of,
                            as_of_kst,
                            available_data_cutoff,
                        ),
                    )
                    inserted_features += self.conn.total_changes - before
        return DomesticPriceStoreResult(inserted_runs, inserted_snapshots, inserted_features)


def collect_and_store(
    *,
    conn: sqlite3.Connection,
    symbols: Iterable[str] = SUPPORTED_SYMBOLS,
    client: DomesticPriceClient | None = None,
    as_of_kst: str,
    available_data_cutoff: str,
) -> DomesticPriceStoreResult:
    collector = DomesticPriceCollector(client=client or KisInvestorFlowClient())
    records = collector.collect_symbols(symbols, available_data_cutoff=available_data_cutoff)
    return DomesticPriceRepository(conn).store_records(
        records,
        as_of_kst=as_of_kst,
        available_data_cutoff=available_data_cutoff,
    )


def _parse_price_row(row: dict[str, Any], cutoff_dt: datetime, symbol: str) -> dict[str, Any] | None:
    lineage_symbol = row.get("stck_shrn_iscd") or row.get("pdno") or row.get("mksc_shrn_iscd")
    if lineage_symbol not in (None, "", symbol):
        return None
    try:
        trade_date = datetime.strptime(str(row.get("stck_bsop_date") or ""), "%Y%m%d").date()
        close = _to_positive_float(row.get("stck_clpr"))
        volume = _to_positive_int(row.get("acml_vol"))
    except (TypeError, ValueError):
        return None
    source_as_of = datetime.combine(trade_date, time(15, 30), tzinfo=KST)
    if source_as_of > cutoff_dt:
        return None
    return {"trade_date": trade_date, "close": close, "volume": volume}


def _to_positive_float(value: Any) -> float:
    if value in (None, ""):
        raise ValueError("empty numeric value")
    parsed = float(str(value).replace(",", ""))
    if parsed <= 0:
        raise ValueError("nonpositive numeric value")
    return parsed


def _to_positive_int(value: Any) -> int:
    if value in (None, ""):
        raise ValueError("empty integer value")
    parsed = int(str(value).replace(",", ""))
    if parsed <= 0:
        raise ValueError("nonpositive integer value")
    return parsed


def _source_as_of(trade_date: date) -> str:
    return datetime.combine(trade_date, time(15, 30), tzinfo=KST).isoformat(timespec="seconds")


def _parse_kst_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone offset")
    return parsed.astimezone(KST)


def _get_xkrx_calendar() -> Any:
    import exchange_calendars as xcals

    return xcals.get_calendar("XKRX")


def _validate_completed_xkrx_session(
    symbol: str, trade_date: date, cutoff_dt: datetime, max_session_lag: int
) -> None:
    """trade_date 가 cutoff 기준 완료(15:30 KST 마감 경과)된 실제 XKRX 세션이며 허용 lag 이내인지 검증.

    lag=0 이면 cutoff 시점의 최신 완료 세션과 정확히 일치해야 한다.
    캘린더 로드/평가 실패 시 XkrxCalendarUnavailableError 로 fail-closed 하며 평일 근사 폴백은 없다.
    """
    cutoff_kst = cutoff_dt.astimezone(KST)
    violation = _completed_session_violation(
        trade_date, cutoff_kst, max_session_lag=max_session_lag, calendar_factory=_get_xkrx_calendar
    )
    if violation == "not_a_session":
        raise StaleDomesticPriceError(
            f"{symbol} domestic price trade date is not an XKRX session: {trade_date.isoformat()}"
        )
    if violation == "not_completed":
        raise StaleDomesticPriceError(
            f"{symbol} domestic price trade date session is not completed at cutoff: {trade_date.isoformat()}"
        )
    if violation == "stale":
        raise StaleDomesticPriceError(
            f"{symbol} domestic price is stale: latest={trade_date.isoformat()}, cutoff={cutoff_kst.date().isoformat()}"
        )


def latest_completed_xkrx_session(now: datetime, *, calendar_factory: Callable[[], Any] | None = None) -> date:
    """now 시점(KST)에 마감(15:30 KST)까지 끝난 가장 최근 XKRX 세션. 실패 시 fail-closed."""
    cutoff_kst = now.astimezone(KST)
    factory = calendar_factory if calendar_factory is not None else _get_xkrx_calendar
    sessions = _xkrx_sessions(
        cutoff_kst.date() - timedelta(days=45), cutoff_kst.date(), calendar_factory=factory
    )
    completed = [
        session
        for session in sessions
        if datetime.combine(session, XKRX_SESSION_CLOSE, tzinfo=KST) <= cutoff_kst
    ]
    if not completed:
        raise XkrxCalendarUnavailableError(
            f"no completed XKRX session within lookback before {cutoff_kst.isoformat()}"
        )
    return completed[-1]


def _sha256_json(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect K-Semiconductor domestic price/volume into SQLite ledger")
    parser.add_argument("--db", required=True, help="SQLite ledger path with ksf_* migration already applied")
    parser.add_argument("--symbols", default=",".join(SUPPORTED_SYMBOLS), help="Comma-separated symbols")
    parser.add_argument("--as-of-kst", required=True, help="KST timestamp used as ingest/cutoff time")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
        result = collect_and_store(conn=conn, symbols=symbols, as_of_kst=args.as_of_kst, available_data_cutoff=args.as_of_kst)
        print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
