"""Read-only global semiconductor, FX, and memory-license collector.

The collector writes only normalized, secret-free metadata/features into the
K-Semiconductor SQLite ledgers.  It intentionally does not persist full raw
vendor responses in Git or logs, and it treats unapproved memory-price sources as
license-blocked metadata rather than scoring inputs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import socket
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, cast
from zoneinfo import ZoneInfo

KST = dt.timezone(dt.timedelta(hours=9))
UTC = dt.timezone.utc
DEFAULT_LEDGER_SYMBOLS = ("005930", "000660")
GLOBAL_PEERS = ("MU", "NVDA", "SOXX")
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
TWSE_STOCK_DAY_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
BOK_ECOS_URL_TEMPLATE = (
    "https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/100/"
    "731Y001/D/{start}/{end}/0000001"
)
FRED_DEXKOUS_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXKOUS"
MEMORY_LICENSE_URL = "https://www.trendforce.com/price/dram/dram_spot"
MEMORY_TERMS_URL = "https://www.dramexchange.com/About/TermsOfUse"
USER_AGENT = "KronosStock-KSF-Collector/0.1 read-only contact=kaos1025"

HttpGet = Callable[[str, Mapping[str, str] | None], tuple[int, str]]
ENV_KEY = object()


@dataclass(frozen=True)
class SourceSnapshot:
    run_id: str | None
    symbol: str | None
    source_name: str
    source_kind: str
    source_status: str
    source_as_of: str
    ingested_at_kst: str
    available_data_cutoff: str
    quality_grade: str
    lag_days: int
    missing_reason: str | None = None
    raw_ref_uri: str | None = None
    raw_ref_sha256: str | None = None
    normalized_payload_sha256: str | None = None
    snapshot_metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def snapshot_id(self) -> str:
        return stable_id(
            "snap",
            {
                "run_id": self.run_id,
                "symbol": self.symbol,
                "source_name": self.source_name,
                "source_kind": self.source_kind,
                "source_as_of": self.source_as_of,
                "available_data_cutoff": self.available_data_cutoff,
                "normalized_payload_sha256": self.normalized_payload_sha256,
                "raw_ref_sha256": self.raw_ref_sha256,
            },
        )


@dataclass(frozen=True)
class NormalizedFeature:
    run_id: str
    symbol: str
    source_snapshot_id: str | None
    feature_group: str
    feature_name: str
    feature_version: str
    feature_status: str
    source_as_of: str
    ingested_at_kst: str
    available_data_cutoff: str
    value_num: float | None = None
    value_text: str | None = None
    value_json: Mapping[str, Any] | list[Any] | None = None
    unit: str | None = None
    contribution_cap_bps: int = 10000
    missing_reason: str | None = None

    @property
    def feature_id(self) -> str:
        return stable_id(
            "feat",
            {
                "run_id": self.run_id,
                "symbol": self.symbol,
                "feature_group": self.feature_group,
                "feature_name": self.feature_name,
                "feature_version": self.feature_version,
            },
        )


@dataclass(frozen=True)
class CollectionResult:
    snapshots_inserted: int
    features_inserted: int
    source_metadata_inserted: int
    statuses: dict[str, str]
    fallback_used: dict[str, str]


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}_{sha256_text(stable_json(payload))[:24]}"


def parse_iso_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value[:10])


def parse_cutoff(value: str) -> dt.datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def is_source_date_available(source_date: dt.date, cutoff: str, *, timezone_name: str, publish_time: dt.time) -> bool:
    """Whether a date-labelled source row could exist by the cutoff instant.

    US peer/FRED rows are labelled by US session/statistic date, not KST date.
    A 2026-07-31 US close is therefore unavailable at a 2026-07-31 16:00 KST
    decision cutoff.  Comparing the source publication instant prevents that
    no-look-ahead leak while still preserving date-only ledger `source_as_of`.
    """
    source_tz = ZoneInfo(timezone_name)
    publish_at = dt.datetime.combine(source_date, publish_time, source_tz)
    return publish_at <= parse_cutoff(cutoff).astimezone(source_tz)


def normalize_timestamp(value: str, *, end_of_day_tz: dt.timezone = KST) -> str:
    """Return an ISO timestamp while preserving date-only source semantics."""
    if "T" in value:
        return value
    return dt.datetime.combine(dt.date.fromisoformat(value), dt.time(23, 59, 59), end_of_day_tz).isoformat()


def days_lag(source_as_of: str, cutoff: str) -> int:
    return max(0, (parse_iso_date(cutoff) - parse_iso_date(source_as_of)).days)


def status_for_lag(lag_days: int, stale_after_days: int) -> str:
    return "STALE" if lag_days > stale_after_days else "CLOSE_CONFIRMED"


def default_http_get(url: str, headers: Mapping[str, str] | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **dict(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec: read-only allowlisted GETs
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        return 599, f"read-only GET failed: {type(exc).__name__}"


def init_ledger_schema(conn: sqlite3.Connection, migration_path: str | Path | None = None) -> None:
    path = Path(migration_path or Path(__file__).resolve().parents[1] / "migrations" / "001_k_semiconductor_flow_desk_permanent_ledgers.sql")
    conn.executescript(path.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")


def ensure_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    symbol: str,
    trading_date: str,
    as_of_kst: str,
    available_data_cutoff: str,
    run_status: str = "PARTIAL_DATA",
) -> None:
    conn.execute(
        """
        INSERT INTO ksf_runs
        (run_id, symbol, trading_date, run_status, as_of_kst, available_data_cutoff,
         scoring_ruleset_version, prompt_template_version, model_policy_version)
        VALUES (?, ?, ?, ?, ?, ?, 'ksf-global-collector-v1', 'none', 'none')
        ON CONFLICT(run_id) DO NOTHING
        """,
        (run_id, symbol, trading_date, run_status, as_of_kst, available_data_cutoff),
    )


def insert_source_metadata(conn: sqlite3.Connection, records: Iterable[Mapping[str, Any]]) -> int:
    before = conn.total_changes
    for record in records:
        payload = dict(record)
        payload.setdefault("metadata_id", stable_id("meta", payload))
        conn.execute(
            """
            INSERT INTO ksf_collected_source_metadata
            (metadata_id, source_name, endpoint_template, method, source_url, terms_url,
             license_scope, rate_limit_policy, user_agent_policy, capture_date,
             quality_grade, raw_storage_policy, redistribution_policy, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(metadata_id) DO NOTHING
            """,
            (
                payload["metadata_id"],
                payload["source_name"],
                payload.get("endpoint_template"),
                payload.get("method", "GET"),
                payload.get("source_url"),
                payload.get("terms_url"),
                payload.get("license_scope", "unverified"),
                payload.get("rate_limit_policy"),
                payload.get("user_agent_policy", USER_AGENT),
                payload["capture_date"],
                payload["quality_grade"],
                payload["raw_storage_policy"],
                payload.get("redistribution_policy", "internal_only_no_raw_redistribution"),
                payload.get("notes"),
            ),
        )
    return conn.total_changes - before


def source_metadata_id_for(conn: sqlite3.Connection, *, source_name: str, capture_date: str) -> str:
    rows = conn.execute(
        """
        SELECT metadata_id FROM ksf_collected_source_metadata
        WHERE source_name = ? AND capture_date = ?
        ORDER BY metadata_id
        """,
        (source_name, capture_date),
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"expected exactly one source metadata row for {source_name!r} on {capture_date}, found {len(rows)}"
        )
    return str(rows[0][0])


def insert_snapshot(conn: sqlite3.Connection, snapshot: SourceSnapshot) -> int:
    metadata_id = source_metadata_id_for(
        conn,
        source_name=snapshot.source_name,
        capture_date=snapshot.available_data_cutoff[:10],
    )
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO ksf_source_snapshots
        (snapshot_id, run_id, symbol, source_metadata_id, source_name, source_kind, source_status,
         source_as_of, ingested_at_kst, available_data_cutoff, quality_grade, lag_days,
         missing_reason, raw_ref_uri, raw_ref_sha256, normalized_payload_sha256,
         snapshot_metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_id) DO NOTHING
        """,
        (
            snapshot.snapshot_id,
            snapshot.run_id,
            snapshot.symbol,
            metadata_id,
            snapshot.source_name,
            snapshot.source_kind,
            snapshot.source_status,
            snapshot.source_as_of,
            snapshot.ingested_at_kst,
            snapshot.available_data_cutoff,
            snapshot.quality_grade,
            snapshot.lag_days,
            snapshot.missing_reason,
            snapshot.raw_ref_uri,
            snapshot.raw_ref_sha256,
            snapshot.normalized_payload_sha256,
            stable_json(snapshot.snapshot_metadata),
        ),
    )
    return conn.total_changes - before


def insert_feature(conn: sqlite3.Connection, feature: NormalizedFeature) -> int:
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO ksf_normalized_features
        (feature_id, run_id, symbol, source_snapshot_id, feature_group, feature_name,
         feature_version, feature_status, value_num, value_text, value_json, unit,
         source_as_of, ingested_at_kst, available_data_cutoff, contribution_cap_bps,
         missing_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(feature_id) DO NOTHING
        """,
        (
            feature.feature_id,
            feature.run_id,
            feature.symbol,
            feature.source_snapshot_id,
            feature.feature_group,
            feature.feature_name,
            feature.feature_version,
            feature.feature_status,
            feature.value_num,
            feature.value_text,
            stable_json(feature.value_json) if feature.value_json is not None else None,
            feature.unit,
            feature.source_as_of,
            feature.ingested_at_kst,
            feature.available_data_cutoff,
            feature.contribution_cap_bps,
            feature.missing_reason,
        ),
    )
    return conn.total_changes - before


def source_metadata_records(capture_date: str) -> list[dict[str, Any]]:
    return [
        {
            "source_name": "alpha_vantage_daily_adjusted",
            "endpoint_template": f"{ALPHA_VANTAGE_URL}?function=TIME_SERIES_DAILY_ADJUSTED&symbol={{symbol}}&outputsize=compact&apikey=$ALPHAVANTAGE_API_KEY",
            "source_url": "https://www.alphavantage.co/documentation/",
            "terms_url": "https://www.alphavantage.co/terms_of_service/",
            "license_scope": "internal_analysis_no_raw_redistribution",
            "rate_limit_policy": "env/tier dependent; collector should batch conservatively",
            "capture_date": capture_date,
            "quality_grade": "B",
            "raw_storage_policy": "GITIGNORED_INTERNAL_ONLY",
            "notes": "MU/NVDA/SOXX candidate source; source lag and market timezone preserved.",
        },
        {
            "source_name": "twse_stock_day_2330",
            "endpoint_template": f"{TWSE_STOCK_DAY_URL}?date={{YYYYMMDD}}&stockNo=2330&response=json",
            "source_url": "https://www.twse.com.tw/en/page/trading/exchange/STOCK_DAY.html",
            "terms_url": "https://www.twse.com.tw/en/terms",
            "license_scope": "internal_analysis_public_exchange_source",
            "rate_limit_policy": "bounded daily GET only",
            "capture_date": capture_date,
            "quality_grade": "A",
            "raw_storage_policy": "GITIGNORED_INTERNAL_ONLY",
            "notes": "TSMC TWSE ordinary shares; not the NYSE TSM ADR.",
        },
        {
            "source_name": "bok_ecos_usdkrw",
            "endpoint_template": "https://ecos.bok.or.kr/api/StatisticSearch/$BOK_ECOS_KEY/json/kr/1/100/731Y001/D/{start}/{end}/0000001",
            "source_url": "https://ecos.bok.or.kr/api/",
            "terms_url": "https://ecos.bok.or.kr/",
            "license_scope": "internal_analysis_public_statistics_source",
            "rate_limit_policy": "API-key protected; do not log key-bearing URL",
            "capture_date": capture_date,
            "quality_grade": "A",
            "raw_storage_policy": "GITIGNORED_INTERNAL_ONLY",
            "notes": "USD/KRW official statistics; falls back to FRED DEXKOUS when key absent/failing.",
        },
        {
            "source_name": "fred_dexkous",
            "endpoint_template": FRED_DEXKOUS_URL,
            "source_url": "https://fred.stlouisfed.org/series/DEXKOUS",
            "terms_url": "https://fred.stlouisfed.org/legal/",
            "license_scope": "fallback_internal_analysis",
            "rate_limit_policy": "bounded CSV GET",
            "capture_date": capture_date,
            "quality_grade": "B",
            "raw_storage_policy": "GITIGNORED_INTERNAL_ONLY",
            "notes": "Fallback USD/KRW noon buying rate; US business-day calendar may differ from KST.",
        },
        {
            "source_name": "trendforce_dramexchange_memory_license_review",
            "endpoint_template": MEMORY_LICENSE_URL,
            "source_url": MEMORY_LICENSE_URL,
            "terms_url": MEMORY_TERMS_URL,
            "license_scope": "unverified_for_investment_prediction_use",
            "rate_limit_policy": "metadata/manual review only; no price-table scraping",
            "capture_date": capture_date,
            "quality_grade": "BLOCKED",
            "raw_storage_policy": "METADATA_ONLY",
            "notes": "Memory price/raw direction indicators are license-blocked before explicit approval.",
        },
        {
            "source_name": "nasdaq_sox_index_license_review",
            "endpoint_template": None,
            "source_url": "https://www.nasdaq.com/solutions/data/nasdaq-data-link/api",
            "terms_url": "https://www.nasdaq.com/solutions/data-link-commercial-terms",
            "license_scope": "license_required_before_automated_sox_use",
            "rate_limit_policy": "not collected by MVP collector",
            "capture_date": capture_date,
            "quality_grade": "BLOCKED",
            "raw_storage_policy": "METADATA_ONLY",
            "notes": "SOXX ETF is MVP proxy; SOX index is blocked until licensing is approved.",
        },
    ]


def alpha_vantage_url(symbol: str, api_key: str) -> str:
    query = urllib.parse.urlencode(
        {
            "function": "TIME_SERIES_DAILY_ADJUSTED",
            "symbol": symbol,
            "outputsize": "compact",
            "apikey": api_key,
        }
    )
    return f"{ALPHA_VANTAGE_URL}?{query}"


def parse_alpha_vantage_daily(payload: Mapping[str, Any], cutoff: str) -> dict[str, Any] | None:
    series = payload.get("Time Series (Daily)")
    if not isinstance(series, Mapping):
        return None
    cutoff_date = parse_iso_date(cutoff)
    candidates: list[tuple[dt.date, Mapping[str, Any]]] = []
    for day, row in series.items():
        try:
            d = dt.date.fromisoformat(day)
        except ValueError:
            continue
        if isinstance(row, Mapping) and is_source_date_available(d, cutoff, timezone_name="America/New_York", publish_time=dt.time(16, 0)) and d <= cutoff_date:
            candidates.append((d, row))
    if not candidates:
        return None
    candidates.sort(reverse=True, key=lambda item: item[0])
    latest_date, latest = candidates[0]
    previous = candidates[1][1] if len(candidates) > 1 else None

    def f(key: str) -> float | None:
        try:
            return float(str(latest[key]).replace(",", ""))
        except (KeyError, TypeError, ValueError):
            return None

    adjusted_close = f("5. adjusted close") or f("4. close")
    prev_adjusted = None
    if previous is not None:
        try:
            prev_adjusted = float(str(previous.get("5. adjusted close") or previous.get("4. close")).replace(",", ""))
        except (TypeError, ValueError):
            prev_adjusted = None
    one_day_return_bps = None
    if adjusted_close is not None and prev_adjusted not in (None, 0):
        one_day_return_bps = ((adjusted_close / prev_adjusted) - 1.0) * 10000.0
    return {
        "source_as_of": latest_date.isoformat(),
        "adjusted_close": adjusted_close,
        "close": f("4. close"),
        "volume": f("6. volume"),
        "one_day_return_bps": one_day_return_bps,
    }


def collect_alpha_vantage_peer(
    peer_symbol: str,
    *,
    cutoff: str,
    ingested_at_kst: str,
    http_get: HttpGet,
    api_key: str | None,
    fixture_payload: Mapping[str, Any] | None = None,
) -> tuple[SourceSnapshot, list[dict[str, Any]]]:
    if not api_key and fixture_payload is None:
        source_as_of = cutoff[:10]
        snapshot = SourceSnapshot(
            None,
            None,
            "alpha_vantage_daily_adjusted",
            "global_price",
            "MISSING",
            source_as_of,
            ingested_at_kst,
            cutoff,
            "B",
            0,
            "missing ALPHAVANTAGE_API_KEY; raw vendor endpoint not called",
            normalized_payload_sha256=sha256_text(f"alpha-missing-{peer_symbol}-{cutoff}"),
            snapshot_metadata={"peer_symbol": peer_symbol, "market_timezone": "America/New_York", "holiday_calendar": "XNYS", "quality_label": "MISSING"},
        )
        return snapshot, [
            {"name": f"{peer_symbol.lower()}_adjusted_close", "status": "MISSING_OPTIONAL", "missing_reason": snapshot.missing_reason},
            {"name": f"{peer_symbol.lower()}_volume", "status": "MISSING_OPTIONAL", "missing_reason": snapshot.missing_reason},
            {"name": f"{peer_symbol.lower()}_one_day_return_bps", "status": "MISSING_OPTIONAL", "missing_reason": snapshot.missing_reason},
        ]
    payload: Mapping[str, Any]
    if fixture_payload is not None:
        payload = fixture_payload
    else:
        status, body = http_get(alpha_vantage_url(peer_symbol, api_key or ""), None)
        if status != 200:
            raise RuntimeError(f"Alpha Vantage {peer_symbol} returned HTTP {status}")
        payload = json.loads(body)
    row = parse_alpha_vantage_daily(payload, cutoff)
    if not row:
        raise RuntimeError(f"Alpha Vantage {peer_symbol} has no daily row before cutoff")
    lag = days_lag(row["source_as_of"], cutoff)
    source_status = status_for_lag(lag, stale_after_days=5)
    normalized_hash = sha256_text(stable_json({"peer_symbol": peer_symbol, **row}))
    snapshot = SourceSnapshot(
        None,
        None,
        "alpha_vantage_daily_adjusted",
        "global_price",
        source_status,
        row["source_as_of"],
        ingested_at_kst,
        cutoff,
        "B",
        lag,
        "latest Alpha Vantage row is stale versus cutoff" if source_status == "STALE" else None,
        normalized_payload_sha256=normalized_hash,
        snapshot_metadata={"peer_symbol": peer_symbol, "market_timezone": "America/New_York", "holiday_calendar": "XNYS", "quality_label": "STALE" if source_status == "STALE" else "CONFIRMED_CLOSE"},
    )
    return snapshot, [
        {"name": f"{peer_symbol.lower()}_adjusted_close", "status": "READY", "value_num": row["adjusted_close"], "unit": "USD"},
        {"name": f"{peer_symbol.lower()}_volume", "status": "READY", "value_num": row["volume"], "unit": "shares"},
        {"name": f"{peer_symbol.lower()}_one_day_return_bps", "status": "READY" if row["one_day_return_bps"] is not None else "MISSING_OPTIONAL", "value_num": row["one_day_return_bps"], "unit": "bps", "missing_reason": "previous close unavailable" if row["one_day_return_bps"] is None else None},
    ]


def parse_twse_stock_day(payload: Mapping[str, Any], cutoff: str) -> dict[str, Any] | None:
    if payload.get("stat") not in ("OK", "很抱歉，沒有符合條件的資料!"):
        return None
    cutoff_date = parse_iso_date(cutoff)
    rows = payload.get("data") or []
    parsed: list[tuple[dt.date, list[Any]]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 9:
            continue
        parts = str(row[0]).split("/")
        if len(parts) != 3:
            continue
        year = int(parts[0]) + 1911
        date = dt.date(year, int(parts[1]), int(parts[2]))
        if is_source_date_available(date, cutoff, timezone_name="Asia/Taipei", publish_time=dt.time(14, 30)):
            parsed.append((date, row))
    if not parsed:
        return None
    parsed.sort(reverse=True, key=lambda item: item[0])
    date, row = parsed[0]

    def num(idx: int) -> float | None:
        try:
            return float(str(row[idx]).replace(",", "").replace("--", ""))
        except (IndexError, TypeError, ValueError):
            return None

    return {"source_as_of": date.isoformat(), "close": num(6), "volume": num(1), "trade_value_twd": num(2)}


def collect_twse_tsm(
    *,
    cutoff: str,
    ingested_at_kst: str,
    http_get: HttpGet,
    fixture_payload: Mapping[str, Any] | None = None,
) -> tuple[SourceSnapshot, list[dict[str, Any]]]:
    if fixture_payload is None:
        date_param = cutoff[:7].replace("-", "") + "01"
        url = f"{TWSE_STOCK_DAY_URL}?{urllib.parse.urlencode({'date': date_param, 'stockNo': '2330', 'response': 'json'})}"
        status, body = http_get(url, None)
        if status != 200:
            raise RuntimeError(f"TWSE STOCK_DAY returned HTTP {status}")
        fixture_payload = json.loads(body)
    if fixture_payload is None:
        raise RuntimeError("TWSE STOCK_DAY payload unavailable")
    row = parse_twse_stock_day(fixture_payload, cutoff)
    if not row:
        raise RuntimeError("TWSE STOCK_DAY has no 2330 row before cutoff")
    lag = days_lag(row["source_as_of"], cutoff)
    source_status = status_for_lag(lag, stale_after_days=5)
    snapshot = SourceSnapshot(
        None,
        None,
        "twse_stock_day_2330",
        "global_price",
        source_status,
        row["source_as_of"],
        ingested_at_kst,
        cutoff,
        "A",
        lag,
        "TWSE 2330 latest row is stale versus cutoff" if source_status == "STALE" else None,
        normalized_payload_sha256=sha256_text(stable_json(row)),
        snapshot_metadata={"peer_symbol": "2330.TW", "market_timezone": "Asia/Taipei", "holiday_calendar": "TWSE", "quality_label": "STALE" if source_status == "STALE" else "CONFIRMED_CLOSE"},
    )
    return snapshot, [
        {"name": "tsm_twse_close", "status": "READY", "value_num": row["close"], "unit": "TWD"},
        {"name": "tsm_twse_volume", "status": "READY", "value_num": row["volume"], "unit": "shares"},
        {"name": "tsm_twse_trade_value", "status": "READY", "value_num": row["trade_value_twd"], "unit": "TWD"},
    ]


def parse_bok_usdkrw(payload: Mapping[str, Any], cutoff: str) -> dict[str, Any] | None:
    rows = ((payload.get("StatisticSearch") or {}).get("row") or []) if isinstance(payload, Mapping) else []
    cutoff_date = parse_iso_date(cutoff)
    parsed: list[tuple[dt.date, float]] = []
    for row in rows:
        try:
            date = dt.date.fromisoformat(str(row["TIME"]))
            value = float(str(row["DATA_VALUE"]).replace(",", ""))
        except (KeyError, TypeError, ValueError):
            continue
        if is_source_date_available(date, cutoff, timezone_name="Asia/Seoul", publish_time=dt.time(16, 0)):
            parsed.append((date, value))
    if not parsed:
        return None
    parsed.sort(reverse=True, key=lambda item: item[0])
    return {"source_as_of": parsed[0][0].isoformat(), "usdkrw": parsed[0][1]}


def parse_fred_usdkrw(csv_text: str, cutoff: str) -> dict[str, Any] | None:
    cutoff_date = parse_iso_date(cutoff)
    parsed: list[tuple[dt.date, float]] = []
    for line in csv_text.splitlines()[1:]:
        if not line.strip():
            continue
        day, value = line.split(",", 1)
        if value.strip() in {".", ""}:
            continue
        try:
            date = dt.date.fromisoformat(day)
            rate = float(value)
        except ValueError:
            continue
        if is_source_date_available(date, cutoff, timezone_name="America/New_York", publish_time=dt.time(16, 0)):
            parsed.append((date, rate))
    if not parsed:
        return None
    parsed.sort(reverse=True, key=lambda item: item[0])
    return {"source_as_of": parsed[0][0].isoformat(), "usdkrw": parsed[0][1]}


def collect_usdkrw(
    *,
    cutoff: str,
    ingested_at_kst: str,
    http_get: HttpGet,
    bok_key: str | None,
    fixture_payload: Mapping[str, Any] | None = None,
    fred_fixture_csv: str | None = None,
) -> tuple[SourceSnapshot, list[dict[str, Any]], str | None]:
    fallback_used = None
    row = None
    source_name = "bok_ecos_usdkrw"
    quality = "A"
    market_timezone = "Asia/Seoul"
    holiday_calendar = "BOK/KRX statistics"
    missing_reason = None
    if fixture_payload is not None:
        row = parse_bok_usdkrw(fixture_payload, cutoff)
    elif bok_key:
        start = (parse_iso_date(cutoff) - dt.timedelta(days=14)).strftime("%Y%m%d")
        end = parse_iso_date(cutoff).strftime("%Y%m%d")
        status, body = http_get(BOK_ECOS_URL_TEMPLATE.format(key=bok_key, start=start, end=end), None)
        if status == 200:
            row = parse_bok_usdkrw(json.loads(body), cutoff)
        else:
            missing_reason = f"BOK ECOS returned HTTP {status}"
    else:
        missing_reason = "missing BOK_ECOS_KEY"
    if row is None:
        source_name = "fred_dexkous"
        quality = "B"
        market_timezone = "America/New_York"
        holiday_calendar = "US business days/FRED"
        fallback_used = "fred_dexkous"
        if fred_fixture_csv is not None:
            row = parse_fred_usdkrw(fred_fixture_csv, cutoff)
        else:
            status, body = http_get(FRED_DEXKOUS_URL, None)
            if status == 200:
                row = parse_fred_usdkrw(body, cutoff)
            else:
                missing_reason = f"{missing_reason}; FRED returned HTTP {status}"
    if row is None:
        row = {"source_as_of": cutoff[:10], "usdkrw": None}
        status = "MISSING"
        missing_reason = missing_reason or "USD/KRW source unavailable"
    else:
        lag = days_lag(row["source_as_of"], cutoff)
        status = status_for_lag(lag, stale_after_days=7)
        missing_reason = "USD/KRW latest source row is stale versus cutoff" if status == "STALE" else None
    lag = days_lag(row["source_as_of"], cutoff)
    snapshot = SourceSnapshot(
        None,
        None,
        source_name,
        "fx",
        status,
        row["source_as_of"],
        ingested_at_kst,
        cutoff,
        quality,
        lag,
        missing_reason,
        normalized_payload_sha256=sha256_text(stable_json({"source": source_name, **row})),
        snapshot_metadata={"pair": "USD/KRW", "market_timezone": market_timezone, "holiday_calendar": holiday_calendar, "quality_label": "STALE" if status == "STALE" else status},
    )
    feature_status = "READY" if row["usdkrw"] is not None and status != "MISSING" else "MISSING_OPTIONAL"
    return snapshot, [{"name": "usdkrw", "status": feature_status, "value_num": row["usdkrw"], "unit": "KRW/USD", "missing_reason": missing_reason}], fallback_used


def memory_license_blocked_snapshot(*, cutoff: str, ingested_at_kst: str) -> tuple[SourceSnapshot, list[dict[str, Any]]]:
    metadata = {
        "source": "trendforce_or_dramexchange_public",
        "source_url": MEMORY_LICENSE_URL,
        "terms_url": MEMORY_TERMS_URL,
        "raw_price_stored": False,
        "license_review_required": True,
        "source_status": "BLOCKED_REVIEW",
        "feature_status": "LICENSE_BLOCKED",
        "license_scope": "unverified_for_investment_prediction_use",
        "display_policy": "do_not_display_or_score",
        "market_timezone": "Asia/Taipei",
        "quality_label": "LICENSE_BLOCKED",
    }
    snapshot = SourceSnapshot(
        None,
        None,
        "trendforce_dramexchange_memory_license_review",
        "memory_license_review",
        "BLOCKED_REVIEW",
        cutoff,
        ingested_at_kst,
        cutoff,
        "BLOCKED",
        0,
        "memory price indicators are not approved for investment prediction/scoring use",
        normalized_payload_sha256=sha256_text(stable_json(metadata)),
        snapshot_metadata=metadata,
    )
    features = [
        {"name": "dram_spot_direction", "status": "LICENSE_BLOCKED", "missing_reason": snapshot.missing_reason},
        {"name": "nand_wafer_direction", "status": "LICENSE_BLOCKED", "missing_reason": snapshot.missing_reason},
        {"name": "hbm_supply_tightness", "status": "LICENSE_BLOCKED", "missing_reason": snapshot.missing_reason},
    ]
    return snapshot, features


def sox_license_blocked_snapshot(*, cutoff: str, ingested_at_kst: str) -> tuple[SourceSnapshot, list[dict[str, Any]]]:
    metadata = {
        "proxy_used": "SOXX",
        "blocked_index": "SOX",
        "license_review_required": True,
        "source_status": "BLOCKED_REVIEW",
        "market_timezone": "America/New_York",
        "quality_label": "LICENSE_BLOCKED",
    }
    snapshot = SourceSnapshot(
        None,
        None,
        "nasdaq_sox_index_license_review",
        "global_price",
        "BLOCKED_REVIEW",
        cutoff,
        ingested_at_kst,
        cutoff,
        "BLOCKED",
        0,
        "SOX index collection requires approved Nasdaq/index-data license; SOXX ETF is MVP proxy",
        normalized_payload_sha256=sha256_text(stable_json(metadata)),
        snapshot_metadata=metadata,
    )
    return snapshot, [{"name": "sox_index_level", "status": "LICENSE_BLOCKED", "missing_reason": snapshot.missing_reason}]


def _is_missing_feature_value(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def collect_global_inputs_for_symbol(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    run_id: str,
    trading_date: str,
    as_of_kst: str,
    available_data_cutoff: str,
    http_get: HttpGet = default_http_get,
    fixtures: Mapping[str, Any] | None = None,
    alpha_vantage_key: str | None | object = ENV_KEY,
    bok_ecos_key: str | None | object = ENV_KEY,
) -> CollectionResult:
    """Collect normalized global inputs for one domestic ledger symbol.

    `fixtures` enables deterministic offline tests. Its keys may include
    `alpha_vantage` (dict by peer), `twse_stock_day_2330`, `bok_ecos_usdkrw`,
    and `fred_dexkous_csv`.
    """
    fixtures = fixtures or {}
    capture_date = available_data_cutoff[:10]
    resolved_alpha_key = cast(str | None, os.getenv("ALPHAVANTAGE_API_KEY") if alpha_vantage_key is ENV_KEY else alpha_vantage_key)
    resolved_bok_key = cast(str | None, os.getenv("BOK_ECOS_KEY") if bok_ecos_key is ENV_KEY else bok_ecos_key)
    ensure_run(conn, run_id=run_id, symbol=symbol, trading_date=trading_date, as_of_kst=as_of_kst, available_data_cutoff=available_data_cutoff)
    metadata_inserted = insert_source_metadata(conn, source_metadata_records(capture_date))
    snapshots_inserted = 0
    features_inserted = 0
    statuses: dict[str, str] = {}
    fallback_used: dict[str, str] = {}

    def persist(snapshot: SourceSnapshot, specs: list[dict[str, Any]], group: str) -> None:
        nonlocal snapshots_inserted, features_inserted
        snapshots_inserted += insert_snapshot(conn, snapshot)
        statuses[snapshot.source_name] = snapshot.source_status
        for spec in specs:
            status = spec.get("status", "READY")
            value_num = spec.get("value_num")
            value_text = spec.get("value_text")
            value_json = spec.get("value_json", snapshot.snapshot_metadata if status in {"LICENSE_BLOCKED", "MISSING_OPTIONAL", "MISSING_REQUIRED"} else None)
            missing_reason = spec.get("missing_reason") or snapshot.missing_reason
            if status == "READY" and _is_missing_feature_value(value_num) and value_text is None and value_json is None:
                status = "MISSING_OPTIONAL"
                missing_reason = missing_reason or f"{spec['name']} value unavailable in latest source row"
                value_num = None
                value_json = snapshot.snapshot_metadata
            feature = NormalizedFeature(
                run_id=run_id,
                symbol=symbol,
                source_snapshot_id=snapshot.snapshot_id,
                feature_group=group,
                feature_name=spec["name"],
                feature_version="v1",
                feature_status="STALE" if snapshot.source_status == "STALE" and status == "READY" else status,
                value_num=value_num,
                value_text=value_text,
                value_json=value_json,
                unit=spec.get("unit"),
                source_as_of=snapshot.source_as_of,
                ingested_at_kst=snapshot.ingested_at_kst,
                available_data_cutoff=snapshot.available_data_cutoff,
                contribution_cap_bps=0 if status == "LICENSE_BLOCKED" else spec.get("contribution_cap_bps", 10000),
                missing_reason=missing_reason,
            )
            features_inserted += insert_feature(conn, feature)

    alpha_fixtures = fixtures.get("alpha_vantage", {})
    for peer in GLOBAL_PEERS:
        try:
            snap, feats = collect_alpha_vantage_peer(
                peer,
                cutoff=available_data_cutoff,
                ingested_at_kst=available_data_cutoff,
                http_get=http_get,
                api_key=resolved_alpha_key,
                fixture_payload=alpha_fixtures.get(peer),
            )
        except Exception as exc:  # network/schema fallback is represented in ledger, not raised
            snap = SourceSnapshot(
                None,
                None,
                "alpha_vantage_daily_adjusted",
                "global_price",
                "MISSING",
                available_data_cutoff,
                available_data_cutoff,
                available_data_cutoff,
                "B",
                0,
                f"Alpha Vantage {peer} collection failed: {type(exc).__name__}",
                normalized_payload_sha256=sha256_text(f"alpha-error-{peer}-{type(exc).__name__}-{available_data_cutoff}"),
                snapshot_metadata={"peer_symbol": peer, "quality_label": "MISSING", "error_class": type(exc).__name__},
            )
            feats = [{"name": f"{peer.lower()}_adjusted_close", "status": "MISSING_OPTIONAL", "missing_reason": snap.missing_reason}]
        persist(snap, feats, "global_semiconductor_price_context")

    try:
        snap, feats = collect_twse_tsm(
            cutoff=available_data_cutoff,
            ingested_at_kst=available_data_cutoff,
            http_get=http_get,
            fixture_payload=fixtures.get("twse_stock_day_2330"),
        )
    except Exception as exc:
        snap = SourceSnapshot(None, None, "twse_stock_day_2330", "global_price", "MISSING", available_data_cutoff, available_data_cutoff, available_data_cutoff, "A", 0, f"TWSE 2330 collection failed: {type(exc).__name__}", normalized_payload_sha256=sha256_text(f"twse-error-{type(exc).__name__}-{available_data_cutoff}"), snapshot_metadata={"peer_symbol": "2330.TW", "quality_label": "MISSING", "error_class": type(exc).__name__})
        feats = [{"name": "tsm_twse_close", "status": "MISSING_OPTIONAL", "missing_reason": snap.missing_reason}]
    persist(snap, feats, "global_semiconductor_price_context")

    snap, feats, fx_fallback = collect_usdkrw(
        cutoff=available_data_cutoff,
        ingested_at_kst=available_data_cutoff,
        http_get=http_get,
        bok_key=resolved_bok_key,
        fixture_payload=fixtures.get("bok_ecos_usdkrw"),
        fred_fixture_csv=fixtures.get("fred_dexkous_csv"),
    )
    if fx_fallback:
        fallback_used["bok_ecos_usdkrw"] = fx_fallback
    persist(snap, feats, "global_fx_context")

    snap, feats = memory_license_blocked_snapshot(cutoff=available_data_cutoff, ingested_at_kst=available_data_cutoff)
    persist(snap, feats, "global_memory_price_context")
    snap, feats = sox_license_blocked_snapshot(cutoff=available_data_cutoff, ingested_at_kst=available_data_cutoff)
    persist(snap, feats, "global_semiconductor_price_context")

    conn.commit()
    return CollectionResult(snapshots_inserted, features_inserted, metadata_inserted, statuses, fallback_used)


def collect_global_inputs(
    conn: sqlite3.Connection,
    *,
    symbols: Iterable[str] = DEFAULT_LEDGER_SYMBOLS,
    trading_date: str,
    as_of_kst: str,
    available_data_cutoff: str,
    fixtures: Mapping[str, Any] | None = None,
    http_get: HttpGet = default_http_get,
) -> dict[str, CollectionResult]:
    results: dict[str, CollectionResult] = {}
    for symbol in symbols:
        run_id = stable_id("run", {"symbol": symbol, "trading_date": trading_date, "cutoff": available_data_cutoff, "collector": "global-v1"})
        results[symbol] = collect_global_inputs_for_symbol(
            conn,
            symbol=symbol,
            run_id=run_id,
            trading_date=trading_date,
            as_of_kst=as_of_kst,
            available_data_cutoff=available_data_cutoff,
            fixtures=fixtures,
            http_get=http_get,
        )
    return results


def offline_fixture() -> dict[str, Any]:
    return {
        "alpha_vantage": {
            "MU": {"Time Series (Daily)": {"2026-07-31": {"4. close": "108.00", "5. adjusted close": "108.00", "6. volume": "1000"}, "2026-07-30": {"4. close": "100.00", "5. adjusted close": "100.00", "6. volume": "900"}}},
            "NVDA": {"Time Series (Daily)": {"2026-07-31": {"4. close": "180.00", "5. adjusted close": "180.00", "6. volume": "2000"}, "2026-07-30": {"4. close": "175.00", "5. adjusted close": "175.00", "6. volume": "1900"}}},
            "SOXX": {"Time Series (Daily)": {"2026-07-25": {"4. close": "250.00", "5. adjusted close": "250.00", "6. volume": "3000"}, "2026-07-24": {"4. close": "248.00", "5. adjusted close": "248.00", "6. volume": "3100"}}},
        },
        "twse_stock_day_2330": {"stat": "OK", "data": [["115/07/31", "41,000,000", "50,000,000,000", "1210.00", "1230.00", "1200.00", "1225.00", "+15.00", "32,000"]]},
        "fred_dexkous_csv": "observation_date,DEXKOUS\n2026-07-30,1382.50\n2026-07-31,1380.10\n",
    }


def run_smoke(live_readonly: bool = False) -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    init_ledger_schema(conn)
    fixtures = None if live_readonly else offline_fixture()
    result = collect_global_inputs_for_symbol(
        conn,
        symbol="005930",
        run_id="smoke-global-005930-20260731",
        trading_date="2026-07-31",
        as_of_kst="2026-07-31T16:10:00+09:00",
        available_data_cutoff="2026-07-31T16:00:00+09:00",
        fixtures=fixtures,
        alpha_vantage_key=os.getenv("ALPHAVANTAGE_API_KEY") if live_readonly else None,
        bok_ecos_key=os.getenv("BOK_ECOS_KEY") if live_readonly else None,
    )
    counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("ksf_source_snapshots", "ksf_normalized_features", "ksf_collected_source_metadata")}
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    return {"result": result.__dict__, "counts": counts, "integrity_check": integrity, "live_readonly": live_readonly}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="K-Semiconductor global input collector")
    parser.add_argument("--smoke", action="store_true", help="run temp in-memory smoke; no production DB writes")
    parser.add_argument("--live-readonly", action="store_true", help="allow bounded public read-only GETs during smoke")
    args = parser.parse_args(argv)
    if args.smoke:
        print(stable_json(run_smoke(live_readonly=args.live_readonly)))
        return 0
    parser.error("only --smoke is currently exposed; import collect_global_inputs_for_symbol for DB integration")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
