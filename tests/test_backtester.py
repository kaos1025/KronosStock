"""tests/test_backtester.py — 순수 함수 백테스트 유틸 단위 테스트.

상승/하락이 섞인 합성 데이터로 total_return, win_rate, max_drawdown, trades 수를 검증한다.
네트워크/API/Redis 없이 결정론적으로 동작한다.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from strategy.backtester import (
    BacktestResult,
    backtest_threshold_signal,
    calculate_metrics,
)


# ── 합성 데이터 ───────────────────────────────────────────────────────
# close: 상승/보합/하락이 섞임.   signal 은 close[t]→close[t+1] 에만 적용.
#   t=0: pred 0.05 ≥ 0.02 → 100→110 = +0.10  (win)
#   t=1: pred -0.01      → 미진입
#   t=2: pred 0.03 ≥ 0.02 → 105→105 =  0.00  (무승부, win 아님)
#   t=3: pred 0.00       → 미진입
#   t=4: pred 0.05 ≥ 0.02 → 120→100 = -0.1667 (loss)
_DATES = pd.bdate_range("2026-01-05", periods=6)
_CLOSE = pd.Series([100.0, 110.0, 105.0, 105.0, 120.0, 100.0], index=_DATES)
_PRED = pd.Series([0.05, -0.01, 0.03, 0.0, 0.05, 0.0], index=_DATES)


def test_backtest_threshold_signal_basic():
    res = backtest_threshold_signal(_CLOSE, _PRED, entry_threshold=0.02,
                                    initial_cash=1_000_000.0)

    assert isinstance(res, BacktestResult)

    # 3개 시점(t=0,2,4)에서 진입.
    assert len(res.trades) == 3

    # equity: 1.10 * 1.00 * (100/120) = 0.916666...
    assert res.total_return == pytest.approx(0.91666667 - 1.0, rel=1e-6)

    # 이익 거래는 t=0 한 건뿐 → 1/3.
    assert res.win_rate == pytest.approx(1.0 / 3.0, rel=1e-6)

    # peak(1.10M) 대비 마지막 낙폭: 0.916667/1.10 - 1 = -0.16667.
    assert res.max_drawdown == pytest.approx(-0.16666667, rel=1e-6)


def test_trades_record_dates_prices_and_return():
    res = backtest_threshold_signal(_CLOSE, _PRED, entry_threshold=0.02)

    first = res.trades[0]
    assert set(first) == {"entry_date", "entry_price", "exit_date",
                          "exit_price", "return"}
    assert first["entry_date"] == _DATES[0]
    assert first["exit_date"] == _DATES[1]
    assert first["entry_price"] == pytest.approx(100.0)
    assert first["exit_price"] == pytest.approx(110.0)
    assert first["return"] == pytest.approx(0.10)

    # 마지막 거래는 손실(120→100).
    last = res.trades[-1]
    assert last["return"] == pytest.approx(100.0 / 120.0 - 1.0, rel=1e-6)


def test_no_signal_keeps_cash():
    # 모든 예측이 임계값 미만 → 거래 없음, 무손익.
    flat_pred = pd.Series([0.0] * 6, index=_DATES)
    res = backtest_threshold_signal(_CLOSE, flat_pred, entry_threshold=0.02)

    assert res.trades == []
    assert res.total_return == pytest.approx(0.0)
    assert res.win_rate == 0.0
    assert res.max_drawdown == pytest.approx(0.0)


def test_accepts_plain_sequences():
    # pandas 없이 list 입력도 동작(인덱스는 정수 라벨).
    res = backtest_threshold_signal(
        [100.0, 110.0, 121.0],
        [0.05, 0.05, 0.0],
        entry_threshold=0.02,
    )
    assert len(res.trades) == 2
    assert res.trades[0]["entry_date"] == 0
    assert res.trades[1]["exit_date"] == 2
    # 1.1 * 1.1 = 1.21 → +21%, 두 거래 모두 이익.
    assert res.total_return == pytest.approx(0.21, rel=1e-6)
    assert res.win_rate == pytest.approx(1.0)


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        backtest_threshold_signal([100.0, 110.0], [0.05])


def test_calculate_metrics_standalone():
    curve = [100.0, 120.0, 90.0, 108.0]
    trades = [{"return": 0.2}, {"return": -0.25}, {"return": 0.2}]
    res = calculate_metrics(curve, trades)

    assert res.total_return == pytest.approx(0.08)        # 108/100 - 1
    assert res.win_rate == pytest.approx(2.0 / 3.0)       # 2 win / 3
    # peak 120 → 90: 90/120 - 1 = -0.25 가 최저 낙폭.
    assert res.max_drawdown == pytest.approx(-0.25)
    assert math.isclose(res.equity_curve[0], 100.0)
