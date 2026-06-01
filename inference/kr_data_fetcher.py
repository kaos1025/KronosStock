"""한국투자증권(python-kis) 일봉 OHLCV 수집 + Redis 버퍼링.

설계 메모 (python-kis v2.1.6, 소스 검증):
  - PyKis 는 실전(real) KisAuth 를 1차 인증으로 요구한다.
  - 모의투자(virtual)는 두 번째 positional 인자로 KisAuth(virtual=True)를 넘겨야
    '서로 다른 모의 계좌번호'를 지정할 수 있다(인라인 kwargs 에는 virtual_account 없음).
  - 시세/차트(OHLCV)는 모의 모드에서도 실전 도메인을 사용 → 데이터 수집엔 실전 키만 필요.
  - python-kis 가 레이트리밋(실전 20/s, 모의 5/s)을 내부 처리하므로 별도 sleep 불필요.

KIS 키가 없거나 조회가 실패하면 FinanceDataReader(무인증)로 자동 폴백한다.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from functools import lru_cache
from io import StringIO
from typing import Optional

import pandas as pd

from common.config import settings
from common.redis_client import get_redis, key

logger = logging.getLogger(__name__)

# OHLCV 표준 컬럼(예측 입력 · Redis 저장 공통)
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]


# ─────────────────────────────────────────────────────────────────────
# KIS 클라이언트
# ─────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def get_kis_client():
    """실전(+선택적 모의) 자격증명으로 PyKis 클라이언트 생성(프로세스 1회).

    실전 키가 비어 있으면 RuntimeError. KIS_USE_VIRTUAL=true 이고 모의 키가
    모두 채워진 경우에만 모의투자(주문) 도메인을 함께 활성화한다.
    """
    if not settings.kis_configured:
        raise RuntimeError(
            "KIS 실전 자격증명이 비어 있습니다. .env 의 "
            "KIS_HTS_ID / KIS_APPKEY / KIS_APPSECRET / KIS_ACCOUNT 를 채우세요. "
            "(python-kis 는 실전 키가 필수)"
        )

    # 지연 import: pykis 미설치 환경에서도 모듈 import 자체는 가능하도록
    from pykis import KisAuth, PyKis

    real = KisAuth(
        id=settings.kis_hts_id,
        appkey=settings.kis_appkey,
        secretkey=settings.kis_appsecret,
        account=settings.kis_account,
        virtual=False,
    )

    # keep_token: True → ~/.pykis/cache, 경로 → 해당 디렉터리에 캐시
    keep_token = (settings.kis_token_dir or True) if settings.kis_keep_token else False

    if settings.kis_use_virtual and settings.kis_virtual_configured:
        virtual = KisAuth(
            id=settings.kis_virtual_hts_id or settings.kis_hts_id,
            appkey=settings.kis_virtual_appkey,
            secretkey=settings.kis_virtual_appsecret,
            account=settings.kis_virtual_account,
            virtual=True,
        )
        logger.info("PyKis init: 실전 + 모의투자(주문) 모드")
        return PyKis(real, virtual, keep_token=keep_token, use_websocket=settings.kis_use_websocket)

    logger.info("PyKis init: 실전 단독 모드(모의 키 미설정 또는 비활성)")
    return PyKis(real, keep_token=keep_token, use_websocket=settings.kis_use_websocket)


# ─────────────────────────────────────────────────────────────────────
# OHLCV 조회
# ─────────────────────────────────────────────────────────────────────
def fetch_daily_ohlcv(
    code: str,
    days: Optional[int] = None,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> pd.DataFrame:
    """일봉 OHLCV 를 표준 DataFrame 으로 반환.

    우선순위: 한국투자증권(python-kis). 실패 시 FinanceDataReader 폴백.

    Args:
        code: 종목코드(예: '005930')
        days: 최근 N 거래일. None 이면 config 의 forecast_lookback_days(기본 300),
              0 이면 전체(상장 이후 / 폴백 기본 범위). start/end 지정 시 무시.
        start, end: 날짜 범위(지정 시 days 보다 우선)

    Returns:
        index=date(tz-naive), columns=OHLCV_COLUMNS(open/high/low/close/volume/amount)
    """
    if days is None:
        days = settings.forecast_lookback_days
    try:
        return _fetch_kis(code, days=days, start=start, end=end)
    except Exception as exc:  # noqa: BLE001 - 폴백을 위해 광범위 캐치
        logger.warning(
            "KIS OHLCV 조회 실패(code=%s): %s → FinanceDataReader 폴백", code, exc
        )
        return _fetch_fdr(code, days=days, start=start, end=end)


def _fetch_kis(code: str, *, days: int, start: Optional[date], end: Optional[date]) -> pd.DataFrame:
    kis = get_kis_client()
    stock = kis.stock(code)
    if start or end:
        chart = stock.chart(start=start, end=end, period="day")
    elif days:
        chart = stock.chart(f"{days}d", period="day")
    else:
        chart = stock.chart(period="day")
    return _normalize(chart.df())


def _fetch_fdr(code: str, *, days: int, start: Optional[date], end: Optional[date]) -> pd.DataFrame:
    import FinanceDataReader as fdr

    if not (start or end):
        end = end or date.today()
        # 거래일 < 달력일 이므로 여유 있게 범위 확보 후 tail 로 자른다
        start = start or (end - timedelta(days=max(days, 120) * 2))
    df = fdr.DataReader(code, start, end)  # cols: Open High Low Close Volume (Change)
    out = _normalize(df)
    return out.tail(days) if days else out


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """소스별 차이를 표준 스키마로 통일한다."""
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    for col in OHLCV_COLUMNS:
        if col not in out.columns:
            out[col] = 0  # amount 등 누락 시 0
    out = out[OHLCV_COLUMNS].astype(float)

    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out.index = idx.normalize()
    out.index.name = "date"
    return out.sort_index()


# ─────────────────────────────────────────────────────────────────────
# Redis 버퍼링
# ─────────────────────────────────────────────────────────────────────
def buffer_to_redis(code: str, df: pd.DataFrame, *, ttl: int = 7 * 24 * 3600) -> str:
    """OHLCV 를 Redis 에 JSON 으로 저장. 키: kronos:stock:ohlcv:daily:<code>."""
    payload = {
        "code": code,
        "rows": int(len(df)),
        "last_date": df.index[-1].strftime("%Y-%m-%d") if len(df) else None,
        "columns": OHLCV_COLUMNS,
        "data": json.loads(df.reset_index().to_json(orient="split", date_format="iso")),
    }
    k = key("ohlcv", "daily", code)
    get_redis().set(k, json.dumps(payload, ensure_ascii=False), ex=ttl)
    return k


def load_from_redis(code: str) -> Optional[pd.DataFrame]:
    """Redis 버퍼에서 OHLCV 복원. 없으면 None."""
    raw = get_redis().get(key("ohlcv", "daily", code))
    if not raw:
        return None
    payload = json.loads(raw)
    df = pd.read_json(StringIO(json.dumps(payload["data"])), orient="split")
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    return df.sort_index()


# ─────────────────────────────────────────────────────────────────────
# 편의 함수
# ─────────────────────────────────────────────────────────────────────
def fetch_and_buffer(code: str, days: Optional[int] = None) -> pd.DataFrame:
    """조회 + Redis 버퍼링을 한 번에. days=None 이면 config 기본(forecast_lookback_days)."""
    df = fetch_daily_ohlcv(code, days=days)
    buffer_to_redis(code, df)
    return df


def fetch_watchlist(days: Optional[int] = None) -> dict[str, int]:
    """워치리스트 전체 수집·버퍼링. days=None 이면 config 기본(forecast_lookback_days).

    반환: {code: 저장된 row 수}.
    """
    results: dict[str, int] = {}
    for code in settings.watchlist:
        try:
            df = fetch_and_buffer(code, days=days)
            results[code] = len(df)
            logger.info("OHLCV 수집 완료: %s (%d rows)", code, len(df))
        except Exception as exc:  # noqa: BLE001
            logger.error("OHLCV 수집 실패: %s: %s", code, exc)
            results[code] = 0
    return results


if __name__ == "__main__":  # 수동 점검: python -m inference.kr_data_fetcher 005930
    import sys

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    target = sys.argv[1] if len(sys.argv) > 1 else (settings.watchlist or ["005930"])[0]
    frame = fetch_daily_ohlcv(target, days=10)
    print(f"\n[{target}] 최근 {len(frame)}일 일봉:")
    print(frame.tail(10).to_string())
