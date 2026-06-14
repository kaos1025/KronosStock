"""strategy/backtester.py — 순수 함수 백테스트 유틸.

네트워크/API/Redis/주문 호출이 전혀 없는 순수 계산만 담는다.
look-ahead 회피: signal[t] 는 close[t]→close[t+1] 수익률에만 적용한다.
백테스트 결과는 수익률(total_return), 승률(win_rate), MDD(max_drawdown)를 반드시 포함한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd


@dataclass
class BacktestResult:
    """백테스트 산출물."""

    total_return: float          # 최종 누적 수익률 (예: 0.12 = +12%)
    win_rate: float              # 이익 거래 비율 (0.0 ~ 1.0)
    max_drawdown: float          # peak 대비 최저 낙폭 (예: -0.1)
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


def calculate_metrics(
    equity_curve: Sequence[float],
    trades: Sequence[dict[str, Any]],
) -> BacktestResult:
    """equity curve + trades 로부터 지표를 계산한다.

    - total_return: equity[-1] / equity[0] - 1
    - win_rate    : return > 0 인 거래 비율
    - max_drawdown: running peak 대비 최저 낙폭 (낙폭 없으면 0.0)
    """
    curve = [float(x) for x in equity_curve]
    trade_list = [dict(t) for t in trades]

    if len(curve) >= 2 and curve[0] != 0.0:
        total_return = curve[-1] / curve[0] - 1.0
    else:
        total_return = 0.0

    if trade_list:
        wins = sum(1 for t in trade_list if t.get("return", 0.0) > 0.0)
        win_rate = wins / len(trade_list)
    else:
        win_rate = 0.0

    max_drawdown = 0.0
    peak = float("-inf")
    for value in curve:
        if value > peak:
            peak = value
        if peak > 0.0:
            drawdown = value / peak - 1.0
            if drawdown < max_drawdown:
                max_drawdown = drawdown

    return BacktestResult(
        total_return=total_return,
        win_rate=win_rate,
        max_drawdown=max_drawdown,
        trades=trade_list,
        equity_curve=curve,
    )


def backtest_threshold_signal(
    close: Sequence[float] | pd.Series | np.ndarray,
    predicted_return: Sequence[float] | pd.Series | np.ndarray,
    *,
    entry_threshold: float = 0.02,
    initial_cash: float = 1_000_000.0,
) -> BacktestResult:
    """단순 임계값 시그널 백테스트.

    predicted_return[t] >= entry_threshold 이면 close[t]→close[t+1] 수익률을
    1일 long 으로 반영하고, 아니면 현금 유지한다(look-ahead 회피).

    단순 검증용 모델이므로 진입/청산은 같은 close 라벨의 다음 구간 수익률로 표현한다.
    실제 운용/모의투자에서는 slippage, 수수료, open[t+1] 진입, Market-On-Close 체결
    가능성 등을 별도 모델링해야 한다. 포지션 사이징도 의도적으로 매 진입마다 현재
    equity 의 100%를 투입하는 전액 복리 가정이다.

    Parameters
    ----------
    close : 종가 시계열. pandas Series 면 index 를 거래 날짜 라벨로 사용한다.
        list/tuple/numpy.ndarray 입력은 0부터 시작하는 정수 라벨을 사용한다.
    predicted_return : 각 시점의 예측 수익률(close 와 같은 길이).
    entry_threshold : 진입 임계값(기본 0.02 = +2%).
    initial_cash : 시작 자본.
    """
    close_idx: list[Any]
    if isinstance(close, pd.Series):
        close_idx = list(close.index)
        close_vals = [float(x) for x in close.to_numpy()]
    else:
        close_arr = np.asarray(close, dtype=float)
        close_vals = [float(x) for x in close_arr]
        close_idx = list(range(len(close_vals)))

    if isinstance(predicted_return, pd.Series):
        pred_vals = [float(x) for x in predicted_return.to_numpy()]
    else:
        pred_vals = [float(x) for x in np.asarray(predicted_return, dtype=float)]

    if len(pred_vals) != len(close_vals):
        raise ValueError(
            f"close({len(close_vals)}) 와 predicted_return({len(pred_vals)}) 길이가 다릅니다."
        )

    equity = initial_cash
    equity_curve: list[float] = [equity]
    trades: list[dict[str, Any]] = []

    # signal[t] -> close[t]→close[t+1] 수익률. 마지막 시점은 다음 종가가 없어 스킵.
    for t in range(len(close_vals) - 1):
        if pred_vals[t] >= entry_threshold:
            entry_price = close_vals[t]
            exit_price = close_vals[t + 1]
            trade_return = exit_price / entry_price - 1.0 if entry_price != 0.0 else 0.0
            equity *= 1.0 + trade_return
            trades.append(
                {
                    "entry_date": close_idx[t],
                    "entry_price": entry_price,
                    "exit_date": close_idx[t + 1],
                    "exit_price": exit_price,
                    "return": trade_return,
                }
            )
        equity_curve.append(equity)

    return calculate_metrics(equity_curve, trades)
