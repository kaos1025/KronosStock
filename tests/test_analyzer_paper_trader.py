"""tests/test_analyzer_paper_trader.py — 시그널/모의투자 순수 함수 테스트.

실제 KIS/Redis/Telegram/주문 API를 호출하지 않는다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from inference.predictor import ForecastResult
from strategy.analyzer import SignalAction, analyze_forecast, analyze_many
from strategy.paper_trader import PaperPortfolio, apply_signal, apply_signals


def _forecast(
    *,
    code: str = "005930",
    last_close: float = 100.0,
    final_median: float = 103.0,
    up_probability: float = 0.6,
) -> ForecastResult:
    return ForecastResult(
        code=code,
        last_close=last_close,
        horizon=3,
        timestamps=pd.DatetimeIndex(["2026-06-15", "2026-06-16", "2026-06-17"]),
        median_close=np.array([101.0, 102.0, final_median]),
        lower_close=np.array([99.0, 98.0, final_median * 0.95]),
        upper_close=np.array([104.0, 105.0, final_median * 1.05]),
        up_probability=up_probability,
        n_paths=20,
        quantiles=(0.1, 0.5, 0.9),
    )


def test_analyze_forecast_buy_hold_sell_from_result():
    buy = analyze_forecast(_forecast(final_median=103.0, up_probability=0.6))
    hold = analyze_forecast(_forecast(final_median=101.0, up_probability=0.52))
    sell = analyze_forecast(_forecast(final_median=97.0, up_probability=0.48))

    assert buy.action == SignalAction.BUY
    assert buy.expected_return == pytest.approx(0.03)
    assert buy.target_price == pytest.approx(103.0)
    assert "expected_return" in buy.reason

    assert hold.action == SignalAction.HOLD
    assert sell.action == SignalAction.SELL


def test_analyze_forecast_from_redis_payload_shape():
    signal = analyze_forecast(
        {
            "code": "000660",
            "last_close": 200.0,
            "horizon": 5,
            "median_close": [201.0, 206.0],
            "lower_close": [195.0, 198.0],
            "upper_close": [208.0, 214.0],
            "up_probability": 0.7,
        }
    )

    assert signal.code == "000660"
    assert signal.action == SignalAction.BUY
    assert signal.expected_return == pytest.approx(0.03)
    assert signal.as_dict()["action"] == "BUY"


def test_analyze_forecast_rejects_missing_or_empty_payload():
    with pytest.raises(ValueError, match="missing keys"):
        analyze_forecast({"code": "005930"})
    with pytest.raises(ValueError, match="must not be empty"):
        analyze_forecast(
            {
                "code": "005930",
                "last_close": 100.0,
                "horizon": 3,
                "median_close": [],
                "lower_close": [99.0],
                "upper_close": [101.0],
                "up_probability": 0.5,
            }
        )


def test_analyze_many_preserves_codes():
    out = analyze_many(
        {
            "005930": _forecast(code="005930", final_median=103.0, up_probability=0.6),
            "035420": _forecast(code="035420", final_median=99.0, up_probability=0.51),
        }
    )
    assert set(out) == {"005930", "035420"}
    assert out["005930"].action == SignalAction.BUY
    assert out["035420"].action == SignalAction.HOLD


def test_paper_trader_buy_caps_position_and_records_order():
    portfolio = PaperPortfolio(cash=1_000_000.0)
    signal = analyze_forecast(_forecast(final_median=103.0, up_probability=0.6))

    order = apply_signal(portfolio, signal, max_position_pct=0.2)

    assert order is not None
    assert order.side == SignalAction.BUY
    assert order.quantity == 2000  # 1,000,000 * 20% / 100
    assert order.notional == pytest.approx(200_000.0)
    assert portfolio.cash == pytest.approx(800_000.0)
    assert portfolio.positions == {"005930": 2000}
    assert portfolio.market_value({"005930": 100.0}) == pytest.approx(1_000_000.0)


def test_paper_trader_hold_and_small_order_are_noop():
    portfolio = PaperPortfolio(cash=5_000.0)
    hold = analyze_forecast(_forecast(final_median=101.0, up_probability=0.52))
    buy = analyze_forecast(_forecast(final_median=103.0, up_probability=0.6))

    assert apply_signal(portfolio, hold) is None
    assert apply_signal(portfolio, buy, min_order_cash=10_000.0) is None
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(5_000.0)


def test_paper_trader_sell_liquidates_existing_position():
    portfolio = PaperPortfolio(cash=800_000.0, positions={"005930": 2000})
    sell = analyze_forecast(_forecast(final_median=97.0, up_probability=0.4))

    order = apply_signal(portfolio, sell, price=95.0)

    assert order is not None
    assert order.side == SignalAction.SELL
    assert order.quantity == 2000
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(990_000.0)


def test_apply_signals_batch_uses_price_map():
    portfolio = PaperPortfolio(cash=1_000_000.0)
    signals = {
        "005930": analyze_forecast(_forecast(code="005930", final_median=103.0, up_probability=0.6)),
        "000660": analyze_forecast(_forecast(code="000660", final_median=201.0, up_probability=0.52)),
    }

    orders = apply_signals(portfolio, signals, prices={"005930": 50.0, "000660": 200.0})

    assert len(orders) == 1
    assert orders[0].code == "005930"
    assert orders[0].quantity == 4000
    assert portfolio.positions == {"005930": 4000}


def test_apply_signal_uses_full_price_map_for_existing_positions():
    # 기존 다른 종목의 평가액을 NAV에 포함해야 max_position_pct가 전역 포트폴리오 기준으로 동작한다.
    portfolio = PaperPortfolio(cash=800_000.0, positions={"000660": 1000})
    signal = analyze_forecast(_forecast(code="005930", final_median=103.0, up_probability=0.6))

    order = apply_signal(
        portfolio,
        signal,
        price=100.0,
        prices={"000660": 200.0, "005930": 100.0},
        max_position_pct=0.2,
    )

    assert order is not None
    # NAV = 800,000 cash + 200,000 existing position. target 20% = 200,000.
    assert order.quantity == 2000
    assert order.notional == pytest.approx(200_000.0)
    assert portfolio.market_value({"000660": 200.0, "005930": 100.0}) == pytest.approx(1_000_000.0)



def test_paper_trader_rejects_invalid_risk_inputs():
    signal = analyze_forecast(_forecast(final_median=103.0, up_probability=0.6))
    portfolio = PaperPortfolio()

    with pytest.raises(ValueError, match="price must be positive"):
        apply_signal(portfolio, signal, price=0.0)
    with pytest.raises(ValueError, match="max_position_pct"):
        apply_signal(portfolio, signal, max_position_pct=0.0)
    with pytest.raises(ValueError, match="min_order_cash"):
        apply_signal(portfolio, signal, min_order_cash=-1.0)
