"""tests/test_scheduler_dry_run.py — scheduler/dry-run 자동화 테스트.

forecast 함수와 Redis를 주입/monkeypatch 하며, KIS/Telegram/실주문 API는 호출하지 않는다.
"""
from __future__ import annotations

import json

import fakeredis
import numpy as np
import pandas as pd
import pytest

from bot import scheduler as sched
from inference.predictor import ForecastResult
from strategy.paper_trader import PaperPortfolio


def _forecast(code: str = "005930", last_close: float = 100.0, final_median: float = 103.0) -> ForecastResult:
    return ForecastResult(
        code=code,
        last_close=last_close,
        horizon=3,
        timestamps=pd.DatetimeIndex(["2026-06-15", "2026-06-16", "2026-06-17"]),
        median_close=np.array([101.0, 102.0, final_median]),
        lower_close=np.array([99.0, 98.0, final_median * 0.95]),
        upper_close=np.array([104.0, 105.0, final_median * 1.05]),
        up_probability=0.6,
        n_paths=20,
        quantiles=(0.1, 0.5, 0.9),
    )


def test_run_dry_run_cycle_uses_injected_forecasts_without_network_or_alert(monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(sched, "get_redis", lambda: redis)

    called = {"forecast": 0}

    def forbidden_forecast():
        called["forecast"] += 1
        raise AssertionError("forecast_func should not be called when forecasts are injected")

    result = sched.run_dry_run_cycle(
        forecasts={"005930": _forecast()},
        forecast_func=forbidden_forecast,
        send_alert=False,
    )

    assert called["forecast"] == 0
    assert set(result.signals) == {"005930"}
    assert result.orders and result.orders[0].code == "005930"
    assert "KronosStock 시그널" in result.message
    assert result.portfolio_key == "kronos:stock:paper:portfolio"
    raw = redis.get(result.portfolio_key)
    assert raw is not None
    assert json.loads(raw)["positions"] == {"005930": 2000}


def test_run_dry_run_cycle_can_use_forecast_func_and_existing_portfolio(monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(sched, "get_redis", lambda: redis)
    redis.set(
        "kronos:stock:paper:portfolio",
        json.dumps({"cash": 800_000.0, "positions": {"000660": 1000}}),
    )

    result = sched.run_dry_run_cycle(
        forecast_func=lambda: {"005930": _forecast()},
        prices={"000660": 200.0},
        persist_portfolio=True,
        send_alert=False,
    )

    assert result.portfolio.positions["000660"] == 1000
    assert result.portfolio.positions["005930"] == 2000
    assert result.portfolio.cash == pytest.approx(600_000.0)


def test_run_dry_run_cycle_send_alert_is_opt_in(monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(sched, "get_redis", lambda: redis)
    sent = []

    async def fake_send_text(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(sched, "send_text", fake_send_text)

    sched.run_dry_run_cycle(forecasts={"005930": _forecast()}, send_alert=False)
    assert sent == []

    sched.run_dry_run_cycle(forecasts={"005930": _forecast(final_median=101.0)}, send_alert=True)
    assert len(sent) == 1
    assert "KronosStock 시그널" in sent[0]


def test_create_scheduler_registers_four_dry_run_jobs(monkeypatch):
    monkeypatch.setattr(sched.settings, "schedule_premarket", "08:50")
    monkeypatch.setattr(sched.settings, "schedule_open", "09:30")
    monkeypatch.setattr(sched.settings, "schedule_midday", "12:00")
    monkeypatch.setattr(sched.settings, "schedule_close", "15:20")

    scheduler = sched.create_scheduler(send_alert=False)
    jobs = scheduler.get_jobs()

    assert len(jobs) == 4
    assert {job.id for job in jobs} == {
        "kronos-dry-run-premarket",
        "kronos-dry-run-open",
        "kronos-dry-run-midday",
        "kronos-dry-run-close",
    }
    assert all(job.kwargs == {"send_alert": False} for job in jobs)


@pytest.mark.parametrize("value", ["24:00", "12:60", "bad", "9"])
def test_parse_hhmm_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        sched._parse_hhmm(value)
