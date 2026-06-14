"""토스증권 Open API read-only market data fetcher.

현재는 주문/계좌 API를 호출하지 않고, OAuth2 client credentials 인증과
시세용 캔들 차트(`/api/v1/candles`) 조회만 담당한다.

KronosStock 표준 OHLCV 스키마:
  open, high, low, close, volume, amount

토스 캔들 응답에는 거래대금(amount)이 없으므로 0.0으로 채운다.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import httpx
import pandas as pd

from common.config import settings
from common.redis_client import key

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]
DEFAULT_BASE_URL = "https://openapi.tossinvest.com"
TOKEN_CACHE_KEY = key("toss", "access_token")


@dataclass
class _TokenCache:
    access_token: str = ""
    expires_at: float = 0.0


_TOKEN_CACHE = _TokenCache()


def toss_configured() -> bool:
    """토스 Open API client credentials 설정 여부."""
    return bool(settings.tossinvest_client_id and settings.tossinvest_client_secret)


def get_access_token(*, client: httpx.Client | None = None) -> str:
    """OAuth2 client credentials access token 발급/프로세스 내 캐시.

    토스 문서상 client당 유효 token은 1개이며 재발급 시 이전 token이 무효화된다.
    따라서 만료 전에는 같은 프로세스에서 캐시된 token을 재사용한다.
    """
    now = time.time()
    cached = _load_cached_token(now=now)
    if cached:
        return cached
    if not toss_configured():
        raise RuntimeError("Toss Open API credentials are not configured")

    close_client = client is None
    client = client or httpx.Client(timeout=settings.tossinvest_timeout)
    try:
        response = client.post(
            _url("/oauth2/token"),
            data={
                "grant_type": "client_credentials",
                "client_id": settings.tossinvest_client_id,
                "client_secret": settings.tossinvest_client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if close_client:
            client.close()

    token = str(payload["access_token"])
    expires_in = int(payload.get("expires_in", 3600))
    _store_cached_token(token, expires_in=expires_in, now=now)
    return token


def _load_cached_token(*, now: float) -> str | None:
    """Redis 공유 캐시를 우선 사용하고, 실패 시 프로세스 메모리 캐시로 fallback."""
    try:
        from common.redis_client import get_redis

        raw = get_redis().get(TOKEN_CACHE_KEY)
        if raw:
            payload = json.loads(raw)
            token = str(payload.get("access_token", ""))
            expires_at = float(payload.get("expires_at", 0.0))
            if token and now < expires_at - 60:
                _TOKEN_CACHE.access_token = token
                _TOKEN_CACHE.expires_at = expires_at
                return token
    except Exception as exc:  # noqa: BLE001 - Redis 없어도 read-only fetcher는 동작해야 한다
        logger.debug("Toss token Redis cache read skipped: %s", exc)

    if _TOKEN_CACHE.access_token and now < _TOKEN_CACHE.expires_at - 60:
        return _TOKEN_CACHE.access_token
    return None


def _store_cached_token(token: str, *, expires_in: int, now: float) -> None:
    expires_at = now + expires_in
    _TOKEN_CACHE.access_token = token
    _TOKEN_CACHE.expires_at = expires_at
    try:
        from common.redis_client import get_redis

        ttl = max(expires_in - 60, 1)
        get_redis().set(
            TOKEN_CACHE_KEY,
            json.dumps({"access_token": token, "expires_at": expires_at}),
            ex=ttl,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Toss token Redis cache write skipped: %s", exc)


def fetch_daily_ohlcv(
    code: str,
    days: Optional[int] = None,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    adjusted: bool = True,
    client: httpx.Client | None = None,
) -> pd.DataFrame:
    """토스 Open API 일봉 캔들을 KronosStock 표준 OHLCV DataFrame으로 반환.

    Args:
        code: KRX 6자리 또는 해외 티커. 예: '005930', 'AAPL'.
        days: 최근 N개 봉. None이면 config forecast_lookback_days 사용.
        start/end: 반환 DataFrame을 날짜 범위로 필터링. 토스 API는 before 기반
            pagination만 제공하므로 필요한 만큼 받은 뒤 로컬에서 필터링한다.
        adjusted: 수정주가 적용 여부. 토스 기본값과 동일하게 True.
        client: 테스트용 httpx.Client 주입 지점.
    """
    if days is None:
        days = settings.forecast_lookback_days
    target_count = max(int(days or 200), 1)

    close_client = client is None
    client = client or httpx.Client(timeout=settings.tossinvest_timeout)
    try:
        candles = _fetch_candles_pages(code, target_count=target_count, adjusted=adjusted, client=client)
    finally:
        if close_client:
            client.close()

    df = _normalize_candles(candles)
    if start is not None:
        df = df[df.index.date >= start]
    if end is not None:
        df = df[df.index.date <= end]
    return df.tail(days) if days else df


def fetch_current_prices(symbols: list[str], *, client: httpx.Client | None = None) -> dict[str, float]:
    """토스 현재가 조회(read-only). 최대 200개 심볼."""
    if not symbols:
        return {}
    if len(symbols) > 200:
        raise ValueError("Toss prices endpoint supports at most 200 symbols")

    close_client = client is None
    client = client or httpx.Client(timeout=settings.tossinvest_timeout)
    try:
        response = client.get(
            _url("/api/v1/prices"),
            params={"symbols": ",".join(symbols)},
            headers=_auth_headers(client=client),
        )
        response.raise_for_status()
        rows = response.json().get("result", [])
    finally:
        if close_client:
            client.close()
    return {str(row["symbol"]): float(row["lastPrice"]) for row in rows}


def _fetch_candles_pages(
    code: str,
    *,
    target_count: int,
    adjusted: bool,
    client: httpx.Client,
) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    before: str | None = None
    while len(candles) < target_count:
        count = min(200, target_count - len(candles))
        params: dict[str, Any] = {
            "symbol": code,
            "interval": "1d",
            "count": count,
            "adjusted": str(adjusted).lower(),
        }
        if before:
            params["before"] = before

        response = client.get(_url("/api/v1/candles"), params=params, headers=_auth_headers(client=client))
        response.raise_for_status()
        result = response.json().get("result", {})
        page = list(result.get("candles", []))
        candles.extend(page)
        before = result.get("nextBefore")
        if not page or not before:
            break
    return candles


def _normalize_candles(candles: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for candle in candles:
        rows.append(
            {
                "date": pd.Timestamp(candle["timestamp"]).tz_localize(None).normalize(),
                "open": float(candle["openPrice"]),
                "high": float(candle["highPrice"]),
                "low": float(candle["lowPrice"]),
                "close": float(candle["closePrice"]),
                "volume": float(candle["volume"]),
                "amount": 0.0,
            }
        )
    if not rows:
        return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([], name="date"))
    df = pd.DataFrame(rows).set_index("date")
    df.index.name = "date"
    return df[OHLCV_COLUMNS].astype(float).sort_index()


def _auth_headers(*, client: httpx.Client) -> dict[str, str]:
    return {"Authorization": f"Bearer {get_access_token(client=client)}"}


def _url(path: str) -> str:
    base_url = settings.tossinvest_base_url.rstrip("/") or DEFAULT_BASE_URL
    return f"{base_url}{path}"


if __name__ == "__main__":  # 수동 점검: python -m inference.toss_data_fetcher 005930
    import sys

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    target = sys.argv[1] if len(sys.argv) > 1 else (settings.watchlist or ["005930"])[0]
    frame = fetch_daily_ohlcv(target, days=10)
    print(f"\n[{target}] Toss 최근 {len(frame)}일 일봉:")
    print(frame.tail(10).to_string())
