"""tests/test_toss_data_fetcher.py — 토스증권 read-only fetcher 단위 테스트.

HTTP는 httpx.MockTransport로 대체하며 실제 토스 API, 계좌, 주문 API는 호출하지 않는다.
"""
from __future__ import annotations

import httpx
import pandas as pd
import pytest

from inference import kr_data_fetcher, toss_data_fetcher


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "test-token", "token_type": "Bearer", "expires_in": 86400})


def _candle(ts: str, close: str) -> dict[str, str]:
    return {
        "timestamp": ts,
        "openPrice": str(float(close) - 100),
        "highPrice": str(float(close) + 200),
        "lowPrice": str(float(close) - 300),
        "closePrice": close,
        "volume": "3521000",
        "currency": "KRW",
    }


@pytest.fixture(autouse=True)
def toss_settings(monkeypatch):
    monkeypatch.setattr(toss_data_fetcher.settings, "tossinvest_client_id", "client-id")
    monkeypatch.setattr(toss_data_fetcher.settings, "tossinvest_client_secret", "client-secret")
    monkeypatch.setattr(toss_data_fetcher.settings, "tossinvest_base_url", "https://openapi.tossinvest.com")
    monkeypatch.setattr(toss_data_fetcher.settings, "tossinvest_timeout", 1.0)
    toss_data_fetcher._TOKEN_CACHE.access_token = ""
    toss_data_fetcher._TOKEN_CACHE.expires_at = 0.0
    yield
    toss_data_fetcher._TOKEN_CACHE.access_token = ""
    toss_data_fetcher._TOKEN_CACHE.expires_at = 0.0


def test_get_access_token_uses_client_credentials_and_caches():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://openapi.tossinvest.com/oauth2/token"
        body = request.content.decode()
        assert "grant_type=client_credentials" in body
        assert "client_id=client-id" in body
        assert "client_secret=client-secret" in body
        return _token_response()

    client = _client(handler)

    assert toss_data_fetcher.get_access_token(client=client) == "test-token"
    assert toss_data_fetcher.get_access_token(client=client) == "test-token"
    assert len(calls) == 1


def test_fetch_daily_ohlcv_reads_toss_candles_and_normalizes():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params), request.headers.get("authorization")))
        if request.url.path == "/oauth2/token":
            return _token_response()
        assert request.url.path == "/api/v1/candles"
        assert request.url.params["symbol"] == "005930"
        assert request.url.params["interval"] == "1d"
        assert request.url.params["count"] == "2"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json={
                "result": {
                    "candles": [
                        _candle("2026-03-25T09:00:00+09:00", "72000"),
                        _candle("2026-03-24T09:00:00+09:00", "71000"),
                    ],
                    "nextBefore": None,
                }
            },
        )

    df = toss_data_fetcher.fetch_daily_ohlcv("005930", days=2, client=_client(handler))

    assert list(df.columns) == ["open", "high", "low", "close", "volume", "amount"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert list(df.index.strftime("%Y-%m-%d")) == ["2026-03-24", "2026-03-25"]
    assert df["close"].tolist() == [71000.0, 72000.0]
    assert df["amount"].tolist() == [0.0, 0.0]
    assert [path for _, path, _, _ in seen] == ["/oauth2/token", "/api/v1/candles"]


def test_fetch_daily_ohlcv_paginates_with_next_before():
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return _token_response()
        pages.append(dict(request.url.params))
        if len(pages) == 1:
            assert request.url.params["count"] == "200"
            assert "before" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "result": {
                        "candles": [_candle(f"2026-03-{day:02d}T09:00:00+09:00", "72000") for day in range(25, 0, -1)]
                        * 8,
                        "nextBefore": "2026-03-01T09:00:00+09:00",
                    }
                },
            )
        assert request.url.params["count"] == "50"
        assert request.url.params["before"] == "2026-03-01T09:00:00+09:00"
        return httpx.Response(
            200,
            json={
                "result": {
                    "candles": [_candle(f"2026-02-{day:02d}T09:00:00+09:00", "71000") for day in range(25, 0, -1)]
                    * 2,
                    "nextBefore": None,
                }
            },
        )

    df = toss_data_fetcher.fetch_daily_ohlcv("005930", days=250, client=_client(handler))

    assert len(pages) == 2
    assert len(df) == 250
    assert df.index.is_monotonic_increasing


def test_fetch_current_prices_batches_symbols():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return _token_response()
        assert request.url.path == "/api/v1/prices"
        assert request.url.params["symbols"] == "005930,000660"
        return httpx.Response(
            200,
            json={"result": [{"symbol": "005930", "lastPrice": "72000"}, {"symbol": "000660", "lastPrice": "150000"}]},
        )

    prices = toss_data_fetcher.fetch_current_prices(["005930", "000660"], client=_client(handler))

    assert prices == {"005930": 72000.0, "000660": 150000.0}


def test_kr_data_fetcher_uses_toss_provider(monkeypatch):
    captured = {}

    def fake_toss(code, *, days, start, end):
        captured.update({"code": code, "days": days, "start": start, "end": end})
        return pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [100.0], "amount": [0.0]},
            index=pd.DatetimeIndex(["2026-03-25"], name="date"),
        )

    monkeypatch.setattr(kr_data_fetcher.settings, "market_data_provider", "toss")
    monkeypatch.setattr(kr_data_fetcher, "_fetch_toss", fake_toss)

    df = kr_data_fetcher.fetch_daily_ohlcv("005930", days=5)

    assert captured == {"code": "005930", "days": 5, "start": None, "end": None}
    assert df["close"].iloc[-1] == 1.5
