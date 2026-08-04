"""삼성전자·SK하이닉스 국내 투자자 수급 collector.

범위:
- KIS read-only quotation endpoints only. 주문/계좌/잔고 endpoint를 호출하지 않는다.
- 외국인, 기관계, KIS 원천 taxonomy의 `기금` 데이터를 수집한다.
- SQLite 영구 원장(`ksf_*`)에 종목별 독립 run/source/feature로 idempotent 저장한다.

주의: KIS `fund_*`는 KRX `연기금 등` 또는 국민연금과 동일시하지 않고
`kis_fund` / `KIS_FUND` taxonomy 그대로 저장한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from common.config import settings

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
XKRX_SESSION_CLOSE = dt_time(15, 30)
DEFAULT_SYMBOLS = ("005930", "000660")
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
KIS_READONLY_ENDPOINTS = {
    "/uapi/domestic-stock/v1/quotations/inquire-investor": "FHKST01010900",
    "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily": "FHPTJ04160001",
    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice": "FHKST03010100",
}


class StaleDataError(RuntimeError):
    """최신 source row가 허용 lag를 초과했을 때 발생."""


class InvalidRequiredQuantityError(RuntimeError):
    """필수 수급 수량(frgn/orgn/fund ntby_qty)이 누락·빈값·비정상일 때 fail-closed."""


class XkrxCalendarUnavailableError(RuntimeError):
    """XKRX 세션 캘린더를 로드/평가할 수 없을 때 fail-closed. 평일 근사 폴백 금지."""


class InvestorFlowClient(Protocol):
    def fetch_inquire_investor(self, symbol: str) -> list[dict[str, str]]: ...

    def fetch_institution_daily(self, symbol: str, trade_date: date | None = None) -> list[dict[str, str]]: ...

    def fetch_daily_itemchartprice(self, symbol: str, *, start_date: str, end_date: str) -> list[dict[str, str]]: ...


@dataclass(frozen=True)
class InvestorFlowRecord:
    symbol: str
    trade_date: date
    investor_category: str
    source_taxonomy: str
    net_buy_quantity: int
    net_buy_amount_million_krw: int | None
    source_status: str
    source_name: str


@dataclass(frozen=True)
class StoreResult:
    inserted_runs: int
    inserted_snapshots: int
    inserted_features: int


class RateLimiter:
    """간단한 동기 rate limiter. 첫 호출은 즉시, 이후 호출 사이 최소 간격 보장."""

    def __init__(
        self,
        min_interval_seconds: float = 1.2,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval_seconds = min_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call_at: float | None = None

    def wait(self) -> None:
        if self._last_call_at is None:
            self._last_call_at = self._monotonic()
            return
        elapsed = self._monotonic() - self._last_call_at
        remaining = self.min_interval_seconds - elapsed
        if remaining > 0:
            self._sleep(remaining)
        self._last_call_at = self._monotonic()


class KisInvestorFlowClient:
    """KIS read-only 국내주식 투자자 수급 REST client.

    테스트 가능하도록 http_client와 rate_limiter를 주입받는다. 기본 http_client는
    httpx.Client(base_url=KIS_BASE_URL)이며 POST tokenP + GET quotation endpoint만 사용한다.
    """

    def __init__(
        self,
        *,
        appkey: str | None = None,
        appsecret: str | None = None,
        base_url: str = KIS_BASE_URL,
        http_client: Any | None = None,
        rate_limiter: RateLimiter | None = None,
        timeout: float = 10.0,
        token_cache_path: str | Path | None = None,
        keep_token: bool | None = None,
    ) -> None:
        self.appkey = appkey if appkey is not None else settings.kis_appkey
        self.appsecret = appsecret if appsecret is not None else settings.kis_appsecret
        if not self.appkey or not self.appsecret:
            raise RuntimeError("KIS_APPKEY/KIS_APPSECRET are required for investor-flow smoke collection")
        if http_client is None:
            import httpx

            http_client = httpx.Client(base_url=base_url, timeout=timeout)
        self.http = http_client
        self.rate_limiter = rate_limiter or RateLimiter()
        self._access_token = ""
        self._token_expires_at = 0.0
        resolved_keep_token = settings.kis_keep_token if keep_token is None else keep_token
        self._token_cache_path = _resolve_token_cache_path(token_cache_path, self.appkey) if resolved_keep_token else None

    def _token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token
        cached = _load_cached_token(self._token_cache_path, now=now)
        if cached is not None:
            self._access_token, self._token_expires_at = cached
            return self._access_token
        response = self.http.post(
            "/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": self.appkey, "appsecret": self.appsecret},
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise RuntimeError("KIS tokenP response did not include access_token")
        self._access_token = token
        self._token_expires_at = now + int(payload.get("expires_in") or 86400)
        _store_cached_token(self._token_cache_path, token=token, expires_at=self._token_expires_at)
        return token

    def _get_readonly(self, path: str, *, tr_id: str, params: dict[str, str]) -> dict[str, Any]:
        if KIS_READONLY_ENDPOINTS.get(path) != tr_id:
            raise ValueError(f"KIS endpoint/TR is not allowlisted for read-only investor flow: {path} ({tr_id})")
        self.rate_limiter.wait()
        response = self.http.get(
            path,
            headers={
                "authorization": f"Bearer {self._token()}",
                "appkey": self.appkey,
                "appsecret": self.appsecret,
                "tr_id": tr_id,
                "custtype": "P",
            },
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("rt_cd", "0")) not in {"0", ""}:
            raise RuntimeError(f"KIS {tr_id} error: {payload.get('msg_cd')} {payload.get('msg1')}")
        return payload

    def fetch_inquire_investor(self, symbol: str) -> list[dict[str, str]]:
        payload = self._get_readonly(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            tr_id="FHKST01010900",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        return _coerce_output_rows(payload.get("output"))

    def fetch_institution_daily(self, symbol: str, trade_date: date | None = None) -> list[dict[str, str]]:
        # KIS rejects the request if FID_ETC_CLS_CODE is omitted.  The validated
        # official sample for this endpoint uses blank FID_ORG_ADJ_PRC and
        # FID_ETC_CLS_CODE values, not "0".
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_ORG_ADJ_PRC": "",
            "FID_ETC_CLS_CODE": "",
        }
        if trade_date is not None:
            params["FID_INPUT_DATE_1"] = trade_date.strftime("%Y%m%d")
        payload = self._get_readonly(
            "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily",
            tr_id="FHPTJ04160001",
            params=params,
        )
        return _coerce_output_rows(payload.get("output2") or payload.get("output"))

    def fetch_daily_itemchartprice(self, symbol: str, *, start_date: str, end_date: str) -> list[dict[str, str]]:
        payload = self._get_readonly(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            tr_id="FHKST03010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        return _coerce_output_rows(payload.get("output2") or payload.get("output"))


class InvestorFlowCollector:
    def __init__(
        self,
        *,
        client: InvestorFlowClient,
        stale_after_days: int = 3,
        now: Callable[[], datetime] = lambda: datetime.now(KST),
        max_attempts: int = 3,
        backoff_seconds: float = 1.2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.stale_after_days = stale_after_days
        self.now = now
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep

    def collect_symbol(self, symbol: str) -> list[InvestorFlowRecord]:
        inquire_rows = self._retry(lambda: self.client.fetch_inquire_investor(symbol))
        if not inquire_rows:
            raise RuntimeError(f"KIS inquire-investor returned no rows for {symbol}")
        latest_inquire = _latest_by_trade_date(inquire_rows)
        trade_date = _parse_kis_date(latest_inquire["stck_bsop_date"])
        cutoff = self.now()
        if cutoff.tzinfo is None:
            raise ValueError("InvestorFlowCollector now() must return a timezone-aware datetime")
        cutoff_kst = cutoff.astimezone(KST)
        today = cutoff_kst.date()
        if trade_date > today:
            raise StaleDataError(
                f"{symbol} investor flow has future trade date: latest={trade_date.isoformat()}, today={today.isoformat()}"
            )
        _validate_completed_xkrx_session(
            symbol=symbol,
            trade_date=trade_date,
            cutoff=cutoff_kst,
            max_session_lag=self.stale_after_days,
            calendar_factory=_get_xkrx_calendar,
        )

        institution_rows = self._retry(lambda: self.client.fetch_institution_daily(symbol, trade_date=trade_date))
        matching_institution = _matching_required(institution_rows, trade_date)
        # KIS broker quotation rows are observations, not official KRX close data,
        # even when KIS returns them for a prior trading day.
        source_status = "INTRADAY_ESTIMATE"

        return [
            InvestorFlowRecord(
                symbol=symbol,
                trade_date=trade_date,
                investor_category="foreigner",
                source_taxonomy="KIS_FOREIGNER",
                net_buy_quantity=_to_required_int(latest_inquire.get("frgn_ntby_qty"), field="frgn_ntby_qty", symbol=symbol),
                net_buy_amount_million_krw=_to_optional_int(latest_inquire.get("frgn_ntby_tr_pbmn")),
                source_status=source_status,
                source_name="KIS_INQUIRE_INVESTOR",
            ),
            InvestorFlowRecord(
                symbol=symbol,
                trade_date=trade_date,
                investor_category="institution_total",
                source_taxonomy="KIS_INSTITUTION_TOTAL",
                net_buy_quantity=_to_required_int(latest_inquire.get("orgn_ntby_qty"), field="orgn_ntby_qty", symbol=symbol),
                net_buy_amount_million_krw=_to_optional_int(latest_inquire.get("orgn_ntby_tr_pbmn")),
                source_status=source_status,
                source_name="KIS_INQUIRE_INVESTOR",
            ),
            InvestorFlowRecord(
                symbol=symbol,
                trade_date=trade_date,
                investor_category="kis_fund",
                source_taxonomy="KIS_FUND",
                net_buy_quantity=_to_required_int(matching_institution.get("fund_ntby_qty"), field="fund_ntby_qty", symbol=symbol),
                net_buy_amount_million_krw=_to_optional_int(matching_institution.get("fund_ntby_tr_pbmn")),
                source_status=source_status,
                source_name="KIS_INVESTOR_TRADE_BY_STOCK_DAILY",
            ),
        ]

    def collect_symbols(self, symbols: Iterable[str] = DEFAULT_SYMBOLS) -> list[InvestorFlowRecord]:
        records: list[InvestorFlowRecord] = []
        for symbol in symbols:
            records.extend(self.collect_symbol(symbol))
        return records

    def _retry(self, fn: Callable[[], list[dict[str, str]]]) -> list[dict[str, str]]:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - external client transient wrapper
                last_exc = exc
                if attempt >= self.max_attempts or not _is_transient(exc):
                    raise
                self.sleep(self.backoff_seconds * attempt)
        raise RuntimeError("unreachable retry state") from last_exc


class InvestorFlowRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def store_records(self, records: Iterable[InvestorFlowRecord], *, as_of_kst: str | None = None) -> StoreResult:
        as_of_kst = as_of_kst or datetime.now(KST).isoformat(timespec="seconds")
        as_of = datetime.fromisoformat(as_of_kst)
        if as_of.tzinfo is None:
            raise ValueError("as_of_kst must include a timezone offset")
        inserted_runs = inserted_snapshots = inserted_features = 0
        with self.conn:
            for record in records:
                cutoff = as_of_kst
                source_as_of = as_of_kst
                run_id = _stable_id("ksf_run", record.symbol, record.trade_date.isoformat(), cutoff, "investor_flow_v1")
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
                        as_of_kst,
                        "investor_flow_collector_v1",
                        "none",
                        "none",
                    ),
                )
                inserted_runs += self.conn.total_changes - before

                capture_date = as_of.date().isoformat()
                metadata_id = _stable_id("ksf_meta", record.source_name, capture_date)
                endpoint = next(
                    path for path, tr_id in KIS_READONLY_ENDPOINTS.items()
                    if (
                        (record.source_name == "KIS_INQUIRE_INVESTOR" and tr_id == "FHKST01010900")
                        or (
                            record.source_name == "KIS_INVESTOR_TRADE_BY_STOCK_DAILY"
                            and tr_id == "FHPTJ04160001"
                        )
                    )
                )
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
                        record.source_name,
                        endpoint,
                        f"{KIS_BASE_URL}{endpoint}",
                        capture_date,
                        f"KIS read-only taxonomy source: {record.source_taxonomy}",
                    ),
                )

                payload = {
                    "symbol": record.symbol,
                    "trade_date": record.trade_date.isoformat(),
                    "investor_category": record.investor_category,
                    "source_taxonomy": record.source_taxonomy,
                    "net_buy_quantity": record.net_buy_quantity,
                    "net_buy_amount_million_krw": record.net_buy_amount_million_krw,
                    "source_name": record.source_name,
                }
                payload_hash = _sha256_json(payload)
                snapshot_id = _stable_id("ksf_src", run_id, record.source_name, record.investor_category, payload_hash)
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
                        record.source_name,
                        "domestic_investor_flow",
                        record.source_status,
                        source_as_of,
                        as_of_kst,
                        cutoff,
                        "B",
                        payload_hash,
                        json.dumps(
                            {
                                "investor_category": record.investor_category,
                                "source_taxonomy": record.source_taxonomy,
                                "amount_unit": "million_krw",
                                "raw_storage_policy": "METADATA_ONLY",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
                inserted_snapshots += self.conn.total_changes - before

                feature_version = f"v1:{payload_hash[:12]}"
                feature_id = _stable_id("ksf_feat", run_id, record.investor_category, "net_buy_quantity", feature_version)
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
                        "domestic_investor_flow",
                        f"{record.investor_category}_net_buy_quantity",
                        feature_version,
                        "READY",
                        float(record.net_buy_quantity),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        "shares",
                        source_as_of,
                        cutoff,
                        cutoff,
                    ),
                )
                inserted_features += self.conn.total_changes - before
        return StoreResult(inserted_runs, inserted_snapshots, inserted_features)


def open_ledger(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def collect_and_store(
    *,
    db_path: str | Path,
    symbols: Iterable[str] = DEFAULT_SYMBOLS,
    client: InvestorFlowClient | None = None,
) -> StoreResult:
    collector = InvestorFlowCollector(client=client or KisInvestorFlowClient())
    records = collector.collect_symbols(symbols)
    conn = open_ledger(db_path)
    try:
        return InvestorFlowRepository(conn).store_records(records)
    finally:
        conn.close()


def _resolve_token_cache_path(token_cache_path: str | Path | None, appkey: str) -> Path:
    if token_cache_path is not None:
        return Path(token_cache_path)
    cache_dir = Path(settings.kis_token_dir or ".cache")
    appkey_hash = hashlib.sha256(appkey.encode("utf-8")).hexdigest()[:12]
    return cache_dir / f"kis_investor_flow_token_{appkey_hash}.json"


def _load_cached_token(path: Path | None, *, now: float) -> tuple[str, float] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        token = str(payload.get("access_token") or "")
        expires_at = float(payload.get("expires_at") or 0.0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if token and now < expires_at - 60:
        return token, expires_at
    return None


def _store_cached_token(path: Path | None, *, token: str, expires_at: float) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        payload = json.dumps({"access_token": token, "expires_at": expires_at}, ensure_ascii=False, sort_keys=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning("KIS token cache write failed at %s: %s", path, type(exc).__name__)


def _coerce_output_rows(output: Any) -> list[dict[str, str]]:
    if output is None:
        return []
    if isinstance(output, dict):
        return [{str(k): str(v) for k, v in output.items()}]
    return [{str(k): str(v) for k, v in row.items()} for row in output]


def _latest_by_trade_date(rows: list[dict[str, str]]) -> dict[str, str]:
    return max(rows, key=lambda row: row.get("stck_bsop_date", ""))


def _matching_required(rows: list[dict[str, str]], trade_date: date) -> dict[str, str]:
    if not rows:
        raise RuntimeError("KIS investor-trade-by-stock-daily returned no rows")
    target = trade_date.strftime("%Y%m%d")
    for row in rows:
        if row.get("stck_bsop_date") == target:
            return row
    raise RuntimeError(f"missing KIS fund row for {trade_date.isoformat()}")


def _parse_kis_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _to_required_int(value: Any, *, field: str, symbol: str) -> int:
    """필수 수량 필드 파서. 실수치 0("0")만 0으로 인정하고 누락/빈값/비정상은 fail-closed."""
    if value is None or str(value).strip() == "":
        raise InvalidRequiredQuantityError(f"{symbol} required KIS quantity {field} is missing/empty")
    try:
        return int(str(value).replace(",", ""))
    except ValueError as exc:
        raise InvalidRequiredQuantityError(
            f"{symbol} required KIS quantity {field} is malformed: {value!r}"
        ) from exc


def _to_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(str(value).replace(",", ""))


def _is_transient(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and status_code in {429, 500, 502, 503, 504}:
        return True
    text = str(exc).lower()
    return any(token in text for token in ("egw00201", "rate limit", "timeout", "temporar", "500", "502", "503", "504"))


def _get_xkrx_calendar() -> Any:
    import exchange_calendars as xcals

    return xcals.get_calendar("XKRX")


def _session_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    to_date = getattr(value, "date", None)
    if callable(to_date):
        return to_date()
    raise TypeError(f"unsupported XKRX session value: {value!r}")


def _xkrx_sessions(start: date, end: date, *, calendar_factory: Callable[[], Any]) -> list[date]:
    """[start, end] 구간의 XKRX 세션 date 목록. 캘린더 로드/평가 실패 시 fail-closed."""
    try:
        calendar = calendar_factory()
        raw_sessions = calendar.sessions_in_range(start.isoformat(), end.isoformat())
        return sorted({_session_date(session) for session in raw_sessions})
    except Exception as exc:  # noqa: BLE001 - 어떤 캘린더 실패든 평일 폴백 없이 fail-closed
        raise XkrxCalendarUnavailableError(
            f"XKRX calendar unavailable: {type(exc).__name__}: {exc}"
        ) from exc


def _completed_session_violation(
    trade_date: date,
    cutoff: datetime,
    *,
    max_session_lag: int,
    calendar_factory: Callable[[], Any],
) -> str | None:
    """XKRX 완료 세션 규칙 위반 코드. None=통과.

    '완료'는 해당 세션의 15:30 KST 마감이 cutoff 이전에 지났음을 뜻한다.
    lag=0 이면 trade_date 는 cutoff 시점의 최신 완료 세션과 정확히 일치해야 한다.
    """
    cutoff_kst = cutoff.astimezone(KST)
    start = min(trade_date, cutoff_kst.date() - timedelta(days=45))
    sessions = _xkrx_sessions(start, cutoff_kst.date(), calendar_factory=calendar_factory)
    if trade_date not in sessions:
        return "not_a_session"
    completed = [
        session
        for session in sessions
        if datetime.combine(session, XKRX_SESSION_CLOSE, tzinfo=KST) <= cutoff_kst
    ]
    if trade_date not in completed:
        return "not_completed"
    if sum(1 for session in completed if session > trade_date) > max_session_lag:
        return "stale"
    return None


def _validate_completed_xkrx_session(
    *,
    symbol: str,
    trade_date: date,
    cutoff: datetime,
    max_session_lag: int,
    calendar_factory: Callable[[], Any],
) -> None:
    violation = _completed_session_violation(
        trade_date, cutoff, max_session_lag=max_session_lag, calendar_factory=calendar_factory
    )
    if violation == "not_a_session":
        raise StaleDataError(
            f"{symbol} investor flow trade date is not an XKRX session: {trade_date.isoformat()}"
        )
    if violation == "not_completed":
        raise StaleDataError(
            f"{symbol} investor flow trade date session is not completed at cutoff: {trade_date.isoformat()}"
        )
    if violation == "stale":
        raise StaleDataError(
            f"{symbol} investor flow is stale: latest={trade_date.isoformat()}, "
            f"cutoff={cutoff.astimezone(KST).isoformat()}"
        )


def _sha256_json(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect K-Semiconductor domestic investor flow into SQLite ledger")
    parser.add_argument("--db", required=True, help="SQLite ledger path with ksf_* migration already applied")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="Comma-separated symbols")
    parser.add_argument("--smoke", action="store_true", help="Print secret-free collection summary")
    args = parser.parse_args(argv)

    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    result = collect_and_store(db_path=args.db, symbols=symbols)
    if args.smoke:
        print(
            json.dumps(
                {
                    "symbols": symbols,
                    "inserted_runs": result.inserted_runs,
                    "inserted_snapshots": result.inserted_snapshots,
                    "inserted_features": result.inserted_features,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
