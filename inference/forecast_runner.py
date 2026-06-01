"""
inference/forecast_runner.py — kr_data_fetcher(Redis) → predictor → Redis 글루.

파이프라인:
  Redis(OHLCV)  --load-->  KronosForecaster.predict_probabilistic  -->  ForecastResult
                --buffer-->  Redis(forecast)

CPU 성능을 위해 KronosForecaster 를 프로세스당 1회만 로드(싱글톤)하고 여러 종목에 재사용한다
(종목마다 모델 재로딩 금지).

Redis 키 네임스페이스(입력/출력 분리):
  입력: kronos:stock:ohlcv:daily:<code>     (kr_data_fetcher.buffer_to_redis)
  출력: kronos:stock:forecast:daily:<code>  (buffer_forecast_to_redis)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from functools import lru_cache
from typing import Optional

import pandas as pd

from common.config import settings
from common.redis_client import get_redis, key
from inference import kr_data_fetcher
from inference.predictor import _MODEL_TOKENIZER_MAP, ForecastResult, KronosForecaster

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# KronosForecaster 싱글톤 (프로세스당 1회 로드 → 모든 종목 재사용)
# ─────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def get_forecaster() -> KronosForecaster:
    """모델/토크나이저/Predictor 를 1회 로드한 KronosForecaster 반환(프로세스 싱글톤).

    알려진 모델(predictor._MODEL_TOKENIZER_MAP 등재)은 predictor 의 모델↔토크나이저
    매핑을 '단일 출처'로 사용한다 — config 가 토크나이저/컨텍스트를 잘못 지정해도 매핑이
    조용히 깨지지 않도록 명시 override 를 넘기지 않는다. 미등재(커스텀/파인튜닝) 모델만
    config 의 kronos_tokenizer_repo / kronos_max_context 를 명시 전달한다.
    최초 호출 시 HuggingFace Hub 에서 가중치를 내려받는다(인터넷 필요, CPU 가능).

    주의(싱글톤): @lru_cache 는 단일 프로세스·단일 스레드 일배치 기준이다. kronos_* 설정을
    런타임 중 바꾸면 get_forecaster.cache_clear() 를 호출해야 반영된다. 동시(멀티스레드)
    최초 호출 시 중복 로드 가능성이 있으므로, 동시 호출 경로(예: 웹 요청)가 생기면 락으로 보호할 것.
    """
    model = settings.kronos_model_repo
    if model in _MODEL_TOKENIZER_MAP:
        # 등재 모델: predictor 의 매핑이 토크나이저/컨텍스트를 결정(override 하지 않음)
        fc = KronosForecaster(model_name=model, device=settings.kronos_device)
    else:
        # 미등재(커스텀/파인튜닝) 모델: predictor 가 토크나이저를 요구하므로 config 값 명시
        fc = KronosForecaster(
            model_name=model,
            tokenizer_name=settings.kronos_tokenizer_repo,
            device=settings.kronos_device,
            max_context=settings.kronos_max_context,
        )
    fc.load()
    return fc


# ─────────────────────────────────────────────────────────────────────
# Redis I/O
# ─────────────────────────────────────────────────────────────────────
def load_ohlcv_from_redis(code: str) -> pd.DataFrame:
    """Redis 버퍼에서 OHLCV DataFrame 복원.

    kr_data_fetcher.load_from_redis 를 재사용한다 — buffer_to_redis 의 직렬화
    포맷(orient='split', date 인덱스)의 정확한 역(逆)이 그곳에 있으므로 단일 출처로 유지.
    데이터가 없으면 KeyError.
    """
    df = kr_data_fetcher.load_from_redis(code)
    if df is None or df.empty:
        raise KeyError(
            f"Redis 에 OHLCV 가 없습니다: {key('ohlcv', 'daily', code)} "
            f"(먼저 kr_data_fetcher.fetch_and_buffer('{code}') 로 수집·버퍼링하세요)"
        )
    if not isinstance(df.index, pd.DatetimeIndex):
        # 손상/외부 버퍼면 조용히 1970-epoch 으로 빠지지 않고 즉시 실패
        raise TypeError(
            f"OHLCV 인덱스가 DatetimeIndex 가 아닙니다(code={code}): {type(df.index).__name__}"
        )
    return df


def buffer_forecast_to_redis(code: str, result: ForecastResult, *, ttl: int = 3 * 24 * 3600) -> str:
    """ForecastResult 를 JSON 으로 Redis 에 저장. 키: kronos:stock:forecast:daily:<code>.

    타임스탬프는 ISO 문자열, ndarray 는 list 로 직렬화한다. paths_close 는 존재할 때만 포함.
    """
    ts_index = pd.DatetimeIndex(result.timestamps)
    payload = {
        "code": result.code or code,
        "horizon": int(result.horizon),
        "last_close": float(result.last_close),
        "timestamps": [t.isoformat() for t in ts_index],
        "median_close": [float(x) for x in result.median_close],
        "lower_close": [float(x) for x in result.lower_close],
        "upper_close": [float(x) for x in result.upper_close],
        "up_probability": float(result.up_probability),
        "n_paths": int(result.n_paths),
        "quantiles": [float(q) for q in result.quantiles],
        "summary": result.summary(),
        # OS 로컬 타임존 offset 포함(컨테이너 TZ=Asia/Seoul → +09:00). tzdata 패키지 불필요.
        "generated_at": datetime.now().astimezone().isoformat(),
    }
    if result.paths_close is not None:
        payload["paths_close"] = [[float(v) for v in row] for row in result.paths_close]

    k = key("forecast", "daily", code)
    get_redis().set(k, json.dumps(payload, ensure_ascii=False), ex=ttl)
    return k


# ─────────────────────────────────────────────────────────────────────
# 한국 장 휴장일 (exchange_calendars XKRX, 미설치/실패 시 평일 폴백)
# ─────────────────────────────────────────────────────────────────────
def _kr_holidays(start, end) -> Optional[list[pd.Timestamp]]:
    """[start, end] 구간에서 KRX 비거래 영업일(=휴장일) 목록 반환.

    정확도: exchange_calendars 의 XKRX 캘린더(음력/대체공휴일/임시휴장 자동 반영).
    exchange_calendars 미설치 또는 조회 실패 시 None → predictor 가 평일(월~금) 기준으로
    폴백(경고 로깅). 반환값은 predictor._future_index 의 holidays 인자로 그대로 전달된다.
    """
    try:
        import exchange_calendars as xcals
    except Exception as exc:  # noqa: BLE001
        logger.warning("exchange_calendars 미설치 → 휴장일 없이 평일 기준 예측: %s", exc)
        return None
    try:
        cal = xcals.get_calendar("XKRX")
        sessions = pd.DatetimeIndex(cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end)))
        # tz-naive 로 강제: 라이브러리 버전이 tz-aware 를 주면 멤버십이 전부 어긋나
        # (모든 영업일이 휴장일로 오분류) 미래 인덱스가 과도 스킵되는 것을 방지.
        if sessions.tz is not None:
            sessions = sessions.tz_localize(None)
        sessions = sessions.normalize()
        bdays = pd.bdate_range(start, end)
        return [d for d in bdays if d.normalize() not in sessions]
    except Exception as exc:  # noqa: BLE001
        logger.warning("XKRX 휴장일 조회 실패 → 평일 기준 예측: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────
# 예측 실행
# ─────────────────────────────────────────────────────────────────────
def run_forecast(
    code: str,
    horizon: int = 5,
    n_paths: int = 20,
    lookback: Optional[int] = None,
) -> ForecastResult:
    """Redis 의 OHLCV 로 확률적 예측을 수행해 ForecastResult 반환.

    결과 저장은 호출부에서 buffer_forecast_to_redis 로 한다(run_watchlist_forecast 는 자동 저장).
    """
    df = load_ohlcv_from_redis(code)
    last_ts = pd.Timestamp(df.index[-1])
    # 미래 구간 + 휴장일 여유를 덮도록 넉넉한 달력 범위에서 휴장일을 구한다
    hol_end = last_ts + pd.Timedelta(days=horizon * 2 + 21)
    holidays = _kr_holidays(last_ts + pd.Timedelta(days=1), hol_end)

    forecaster = get_forecaster()
    result = forecaster.predict_probabilistic(
        df,
        code=code,
        horizon=horizon,
        n_paths=n_paths,
        lookback=lookback or settings.kronos_lookback,
        freq="B",
        holidays=holidays,
    )
    logger.info(result.summary())
    return result


def run_watchlist_forecast(horizon: int = 5, n_paths: int = 20) -> dict[str, ForecastResult]:
    """워치리스트 전체 예측·버퍼링. 종목 실패는 로깅 후 계속(전체 중단 금지).

    반환: {code: ForecastResult} (성공한 종목만 포함).
    """
    results: dict[str, ForecastResult] = {}
    for code in settings.watchlist:
        try:
            result = run_forecast(code, horizon=horizon, n_paths=n_paths)
            buffer_forecast_to_redis(code, result)
            results[code] = result
        except Exception as exc:  # noqa: BLE001 - 한 종목 실패가 배치 전체를 막지 않도록
            logger.error("예측 실패: %s: %s", code, exc)
    logger.info("워치리스트 예측 완료: %d/%d 성공", len(results), len(settings.watchlist))
    return results


if __name__ == "__main__":  # 수동 통합 점검: python -m inference.forecast_runner 005930
    import sys

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    target = sys.argv[1] if len(sys.argv) > 1 else (settings.watchlist or ["005930"])[0]
    res = run_forecast(target, horizon=settings.kronos_pred_len, n_paths=settings.forecast_n_paths)
    k = buffer_forecast_to_redis(target, res)
    print(res.summary())
    print("buffered ->", k)
