"""tests/test_dashboard_and_alerts.py — dashboard signal endpoint + Telegram formatter 테스트.

Redis 는 fakeredis, Telegram 전송은 호출하지 않는다.
"""
from __future__ import annotations

import json
import sqlite3

import fakeredis
import pytest
from fastapi.testclient import TestClient

from bot.alert_bot import format_orders, format_signal, format_signal_digest
from dashboard import app as dashboard_app
from strategy.analyzer import analyze_forecast
from strategy.paper_trader import PaperPortfolio, apply_signal
from strategy.paper_web_api import get_runtime_status


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
    assert signal_res.json()["name"] == "삼성전자"
    assert signal_res.json()["expected_return"] == pytest.approx(0.03)

    missing_res = client.get("/forecast/000000")
    assert missing_res.status_code == 404


def test_dashboard_paper_portfolio_endpoint(monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    redis.set(
        "kronos:stock:paper:portfolio",
        json.dumps(
            {
                "cash": 600_000.0,
                "positions": {"005930": 2000},
                "orders": [
                    {
                        "code": "005930",
                        "side": "BUY",
                        "quantity": 2000,
                        "price": 100.0,
                        "notional": 200_000.0,
                        "reason": "test",
                        "created_at": "2026-06-14T09:00:00+09:00",
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(dashboard_app, "get_redis", lambda: redis)
    client = TestClient(dashboard_app.app)

    res = client.get("/paper/portfolio")
    assert res.status_code == 200
    body = res.json()
    assert body["cash"] == 600_000.0
    assert body["positions"] == {"005930": 2000}
    assert body["orders"][0]["code"] == "005930"
    # 비밀값/토큰이 응답에 새지 않는다.
    assert "token" not in res.text.lower()


def test_dashboard_paper_portfolio_missing_returns_404(monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(dashboard_app, "get_redis", lambda: redis)
    client = TestClient(dashboard_app.app)

    res = client.get("/paper/portfolio")
    assert res.status_code == 404


def test_dashboard_paper_portfolio_key_matches_scheduler():
    from bot import scheduler as sched

    assert dashboard_app.PAPER_PORTFOLIO_KEY == sched.PAPER_PORTFOLIO_KEY


def test_alert_formatters_do_not_require_telegram_config():
    signal = analyze_forecast(_FORECAST_PAYLOAD)
    portfolio = PaperPortfolio(cash=1_000_000.0)
    order = apply_signal(portfolio, signal)

    signal_text = format_signal(signal)
    digest_text = format_signal_digest([signal])
    order_text = format_orders([order], portfolio) if order is not None else ""

    assert "삼성전자 (005930)" in signal_text
    assert "BUY" in signal_text
    assert "KronosStock 시그널" in digest_text
    assert "삼성전자 (005930)" in order_text
    assert "TELEGRAM_BOT_TOKEN" not in signal_text


def test_empty_alert_formatters():
    assert "생성된 시그널이 없습니다" in format_signal_digest([])
    assert "체결된 주문이 없습니다" in format_orders([])


def _runtime_payload() -> dict:
    return {"status": "ok", "latest_mode": "shadow", "latest_session_id": "2026-08-20",
        "latest_committed_at": "2026-08-20T09:05:00+09:00", "latest_cycle_id": "cycle-safe",
        "latest_snapshot_at": "2026-08-20T09:04:00+09:00",
        "counts": {"nav_snapshots": 1, "decisions": 2, "cycle_commits": 1,
            "proposals": 0, "reviews": 0, "orders": 0, "fills": 0},
        "safety": {"orders_zero": True, "fills_zero": True, "proposals_zero": True,
            "reviews_zero": True, "shadow_mode": True}, "recent_decisions": []}


def test_paper_status_auth_json_and_dashboard_section(monkeypatch):
    monkeypatch.setenv("KSF_READ_TOKEN", "status-test-token")
    monkeypatch.setattr(dashboard_app, "get_paper_runtime_status", _runtime_payload)
    monkeypatch.setattr(dashboard_app, "get_paper_account", lambda: {"status": "no_data"})
    monkeypatch.setattr(dashboard_app, "get_paper_orders", lambda: {"status": "ok", "orders": []})
    monkeypatch.setattr(dashboard_app, "get_paper_decisions",
                        lambda symbol: {"status": "ok", "symbol": symbol, "decisions": []})
    client = TestClient(dashboard_app.app)
    assert client.get("/paper/status").status_code == 401
    headers = {"Authorization": "Bearer status-test-token"}
    response = client.get("/paper/status", headers=headers)
    assert response.status_code == 200 and response.json() == _runtime_payload()
    assert "status-test-token" not in response.text
    page = client.get("/paper", headers=headers)
    assert page.status_code == 200
    assert "섀도우 런타임 상태" in page.text and "섀도우 (실주문 비활성)" in page.text
    assert "관측 전용이며 거래 기능을 제공하지 않습니다." in page.text
    assert "status-test-token" not in page.text


def test_get_runtime_status_from_minimal_sqlite(tmp_path, monkeypatch):
    path = tmp_path / "runtime.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE paper_nav_snapshots(nav_snapshot_id TEXT,account_id TEXT,snapshot_at TEXT);
        CREATE TABLE paper_agent_decisions(decision_id TEXT,account_id TEXT,symbol TEXT,created_at TEXT,action TEXT,reason_code TEXT);
        CREATE TABLE paper_cycle_commits(cycle_id TEXT,account_id TEXT,session_id TEXT,mode TEXT,committed_at TEXT);
        CREATE TABLE paper_order_proposals(proposal_id TEXT,account_id TEXT);
        CREATE TABLE paper_risk_reviews(review_id TEXT,proposal_id TEXT);
        CREATE TABLE paper_orders(order_id TEXT,account_id TEXT);
        CREATE TABLE paper_fills(fill_id TEXT,order_id TEXT);
        INSERT INTO paper_nav_snapshots VALUES('nav-safe','acct','2026-08-20T09:04:00+09:00');
        INSERT INTO paper_agent_decisions VALUES('decision-safe','acct','005930','2026-08-20T09:03:00+09:00','HOLD','NO_EDGE');
        INSERT INTO paper_cycle_commits VALUES('cycle-safe','acct','2026-08-20','shadow','2026-08-20T09:05:00+09:00');
        INSERT INTO paper_order_proposals VALUES('proposal-safe','acct');
        INSERT INTO paper_risk_reviews VALUES('review-safe','proposal-safe');
        INSERT INTO paper_orders VALUES('order-safe','acct');
        INSERT INTO paper_fills VALUES('fill-safe','order-safe');
    """)
    conn.commit(); conn.close()
    monkeypatch.setenv("PAPER_LEDGER_DB_PATH", str(path))
    monkeypatch.setenv("PAPER_ACCOUNT_ID", "acct")
    result = get_runtime_status()
    assert result["status"] == "ok" and result["latest_mode"] == "shadow"
    assert result["counts"] == {"nav_snapshots": 1, "decisions": 1, "cycle_commits": 1,
        "proposals": 1, "reviews": 1, "orders": 1, "fills": 1}
    assert result["safety"] == {"orders_zero": False, "fills_zero": False,
        "proposals_zero": False, "reviews_zero": False, "shadow_mode": True}
    assert result["recent_decisions"][0]["decision_id"] == "decision-safe"
