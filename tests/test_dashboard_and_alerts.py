"""tests/test_dashboard_and_alerts.py — dashboard signal endpoint + Telegram formatter 테스트.

Redis 는 fakeredis, Telegram 전송은 호출하지 않는다.
"""
from __future__ import annotations

import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from bot.alert_bot import format_orders, format_signal, format_signal_digest
from dashboard import app as dashboard_app
from strategy.analyzer import analyze_forecast
from strategy.paper_trader import PaperPortfolio, apply_signal


_FORECAST_PAYLOAD = {
    "code": "005930",
    "horizon": 3,
    "last_close": 100.0,
    "timestamps": ["2026-06-15", "2026-06-16", "2026-06-17"],
    "median_close": [101.0, 102.0, 103.0],
    "lower_close": [99.0, 98.0, 97.0],
    "upper_close": [104.0, 105.0, 106.0],
    "up_probability": 0.6,
    "n_paths": 20,
    "quantiles": [0.1, 0.5, 0.9],
    "summary": "fake summary",
    "generated_at": "2026-06-14T12:00:00+09:00",
}


def test_dashboard_forecast_and_signal_endpoints(monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    redis.set("kronos:stock:forecast:daily:005930", json.dumps(_FORECAST_PAYLOAD))
    monkeypatch.setattr(dashboard_app, "get_redis", lambda: redis)
    client = TestClient(dashboard_app.app)

    forecast_res = client.get("/forecast/005930")
    assert forecast_res.status_code == 200
    assert forecast_res.json()["code"] == "005930"

    signal_res = client.get("/signal/005930")
    assert signal_res.status_code == 200
    assert signal_res.json()["action"] == "BUY"
    assert signal_res.json()["expected_return"] == pytest.approx(0.03)

    missing_res = client.get("/forecast/000000")
    assert missing_res.status_code == 404


def test_alert_formatters_do_not_require_telegram_config():
    signal = analyze_forecast(_FORECAST_PAYLOAD)
    portfolio = PaperPortfolio(cash=1_000_000.0)
    order = apply_signal(portfolio, signal)

    signal_text = format_signal(signal)
    digest_text = format_signal_digest([signal])
    order_text = format_orders([order], portfolio) if order is not None else ""

    assert "005930" in signal_text
    assert "BUY" in signal_text
    assert "KronosStock 시그널" in digest_text
    assert "Paper trading" in order_text
    assert "TELEGRAM_BOT_TOKEN" not in signal_text


def test_empty_alert_formatters():
    assert "생성된 시그널이 없습니다" in format_signal_digest([])
    assert "체결된 주문이 없습니다" in format_orders([])
