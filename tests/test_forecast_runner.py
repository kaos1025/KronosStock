"""tests/test_forecast_runner.py — 네트워크 없는 단위 테스트.

KronosForecaster 는 stub(합성 fan), Redis 는 fakeredis 로 대체한다.
실제 모델 가중치 다운로드/추론은 하지 않는다(그건 Docker(3.11) 통합 검증의 몫).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from inference import forecast_runner, kr_data_fetcher
from inference.predictor import ForecastResult


# ── 합성 OHLCV ────────────────────────────────────────────────────────
def _synthetic_ohlcv(n: int = 120, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp("2026-05-29"), periods=n)
    close = 70000 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, n)))
    return pd.DataFrame(
        {
            "open": np.r_[close[0], close[:-1]],
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
            "amount": (close * rng.integers(1_000_000, 5_000_000, n)).astype(float),
        },
        index=pd.DatetimeIndex(idx, name="date"),
    )


# ── KronosForecaster stub (합성 fan; 모델/네트워크 불필요) ──────────────
class _FakeForecaster:
    def predict_probabilistic(self, df, *, code="", horizon=5, n_paths=20,
                              lookback=None, freq="B", holidays=None, **kw):
        last_close = float(df["close"].iloc[-1])
        last_ts = pd.Timestamp(df.index[-1])
        y_idx = pd.bdate_range(start=last_ts + pd.Timedelta(days=1), periods=horizon)
        rng = np.random.default_rng(0)
        steps = rng.normal(0.0, 0.01, size=(n_paths, horizon)).cumsum(axis=1)
        paths = last_close * (1.0 + steps)
        return ForecastResult(
            code=code, last_close=last_close, horizon=horizon,
            timestamps=pd.DatetimeIndex(y_idx),
            median_close=np.quantile(paths, 0.5, axis=0),
            lower_close=np.quantile(paths, 0.1, axis=0),
            upper_close=np.quantile(paths, 0.9, axis=0),
            up_probability=float(np.mean(paths[:, -1] > last_close)),
            n_paths=n_paths, quantiles=(0.1, 0.5, 0.9),
            paths_close=paths,
        )


# ── fixtures ──────────────────────────────────────────────────────────
@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=True)
    # 두 모듈이 from-import 한 get_redis 바인딩을 모두 동일 인스턴스로 교체
    monkeypatch.setattr(kr_data_fetcher, "get_redis", lambda: client)
    monkeypatch.setattr(forecast_runner, "get_redis", lambda: client)
    return client


@pytest.fixture
def stub_forecaster(monkeypatch):
    monkeypatch.setattr(forecast_runner, "get_forecaster", lambda: _FakeForecaster())


# ── tests ─────────────────────────────────────────────────────────────
def test_ohlcv_redis_roundtrip(fake_redis):
    """buffer_to_redis 직렬화 ↔ load_ohlcv_from_redis 역직렬화 무손실."""
    df = _synthetic_ohlcv()
    kr_data_fetcher.buffer_to_redis("005930", df)
    loaded = forecast_runner.load_ohlcv_from_redis("005930")
    assert list(loaded.columns) == kr_data_fetcher.OHLCV_COLUMNS
    assert len(loaded) == len(df)
    assert isinstance(loaded.index, pd.DatetimeIndex)
    pd.testing.assert_series_equal(
        loaded["close"].reset_index(drop=True),
        df["close"].reset_index(drop=True),
        check_names=False, check_dtype=False,
    )


def test_load_missing_raises(fake_redis):
    with pytest.raises(KeyError):
        forecast_runner.load_ohlcv_from_redis("999999")


def test_run_forecast_and_buffer(fake_redis, stub_forecaster):
    df = _synthetic_ohlcv()
    kr_data_fetcher.buffer_to_redis("005930", df)

    result = forecast_runner.run_forecast("005930", horizon=5, n_paths=8)
    assert result.horizon == 5
    assert len(result.timestamps) == 5
    assert len(result.median_close) == len(result.lower_close) == len(result.upper_close) == 5
    assert 0.0 <= result.up_probability <= 1.0
    # 분위수 밴드 순서: lower <= median <= upper
    assert np.all(result.lower_close <= result.median_close + 1e-9)
    assert np.all(result.median_close <= result.upper_close + 1e-9)

    out_key = forecast_runner.buffer_forecast_to_redis("005930", result)
    assert out_key == "kronos:stock:forecast:daily:005930"
    raw = fake_redis.get(out_key)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["code"] == "005930"
    assert payload["horizon"] == 5
    assert len(payload["timestamps"]) == 5
    assert len(payload["median_close"]) == 5
    assert "up_probability" in payload
    pd.to_datetime(payload["timestamps"])  # ISO 문자열 파싱 가능해야 함


def test_namespace_separation(fake_redis, stub_forecaster):
    """입력(ohlcv) ↔ 출력(forecast) 키 네임스페이스 분리."""
    df = _synthetic_ohlcv()
    in_key = kr_data_fetcher.buffer_to_redis("005930", df)
    result = forecast_runner.run_forecast("005930", horizon=3, n_paths=5)
    out_key = forecast_runner.buffer_forecast_to_redis("005930", result)
    assert in_key == "kronos:stock:ohlcv:daily:005930"
    assert out_key == "kronos:stock:forecast:daily:005930"
    assert in_key != out_key


def test_watchlist_partial_failure(fake_redis, stub_forecaster, monkeypatch):
    """GOOD 만 버퍼링, BAD 는 데이터 없음 → 실패해도 배치는 계속."""
    df = _synthetic_ohlcv()
    kr_data_fetcher.buffer_to_redis("GOOD", df)
    monkeypatch.setattr(forecast_runner.settings, "watchlist", ["GOOD", "BAD"])

    results = forecast_runner.run_watchlist_forecast(horizon=3, n_paths=5)
    assert "GOOD" in results
    assert "BAD" not in results
    assert isinstance(results["GOOD"], ForecastResult)
    assert fake_redis.get("kronos:stock:forecast:daily:GOOD") is not None
    assert fake_redis.get("kronos:stock:forecast:daily:BAD") is None


def test_kr_holidays_graceful():
    """exchange_calendars 설치 여부와 무관하게 예외 없이 None 또는 list 반환."""
    out = forecast_runner._kr_holidays("2026-01-01", "2026-02-01")
    assert out is None or isinstance(out, list)


def test_buffer_forecast_omits_none_paths(fake_redis):
    """production 경로(keep_paths=False → paths_close=None) 직렬화 검증.

    실제 run_forecast 는 keep_paths 를 넘기지 않으므로 paths_close 는 항상 None →
    payload 에 'paths_close' 키가 없어야 한다(스텁이 항상 채우는 경로가 아닌 실제 분기).
    """
    result = ForecastResult(
        code="005930", last_close=70000.0, horizon=3,
        timestamps=pd.bdate_range("2026-06-01", periods=3),
        median_close=np.array([70100.0, 70200.0, 70300.0]),
        lower_close=np.array([69000.0, 69100.0, 69200.0]),
        upper_close=np.array([71000.0, 71100.0, 71200.0]),
        up_probability=0.6, n_paths=20, quantiles=(0.1, 0.5, 0.9),
        paths_close=None,  # = predictor 기본(keep_paths=False)
    )
    out_key = forecast_runner.buffer_forecast_to_redis("005930", result)
    payload = json.loads(fake_redis.get(out_key))
    assert "paths_close" not in payload  # None 분기: 키 생략
    assert payload["median_close"] == [70100.0, 70200.0, 70300.0]
    assert all(
        lo <= md <= up
        for lo, md, up in zip(payload["lower_close"], payload["median_close"], payload["upper_close"])
    )
    assert pd.to_datetime(payload["generated_at"]) is not None  # offset 포함 ISO 파싱 가능


def test_days_resolution_backward_compat(monkeypatch):
    """days: None→config(forecast_lookback_days), 0→그대로(전체), N→그대로(tail N)."""
    captured = {}

    def fake_kis(code, *, days, start, end):
        captured["days"] = days
        return _synthetic_ohlcv(10)

    monkeypatch.setattr(kr_data_fetcher, "_fetch_kis", fake_kis)
    monkeypatch.setattr(kr_data_fetcher.settings, "market_data_provider", "kis")

    kr_data_fetcher.fetch_daily_ohlcv("005930")  # None → config
    assert captured["days"] == kr_data_fetcher.settings.forecast_lookback_days
    kr_data_fetcher.fetch_daily_ohlcv("005930", days=0)  # 0 → 전체
    assert captured["days"] == 0
    kr_data_fetcher.fetch_daily_ohlcv("005930", days=30)  # N → 그대로
    assert captured["days"] == 30


def test_kr_holidays_real_xkrx():
    """exchange_calendars 설치 시 실제 KRX 휴장일을 정확히 제거하는지(tz 무관)."""
    pytest.importorskip("exchange_calendars")
    hols = forecast_runner._kr_holidays("2026-01-01", "2026-02-28")
    assert isinstance(hols, list) and hols
    norm = {pd.Timestamp(d).normalize() for d in hols}
    assert pd.Timestamp("2026-01-01") in norm  # 신정
    assert pd.Timestamp("2026-02-17") in norm  # 설날 당일(음력 1/1)
