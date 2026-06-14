"""strategy/analyzer.py — forecast payload 를 매매 시그널로 변환하는 순수 함수.

네트워크/API/Redis/주문 호출 없이 ForecastResult 또는 Redis JSON payload 를 입력으로 받아
BUY/HOLD/SELL 판단과 설명 문자열을 생성한다. 실제 주문은 Phase 3 paper/live broker 계층이
별도로 담당한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

import numpy as np

from inference.predictor import ForecastResult


class SignalAction(StrEnum):
    """전략 시그널 액션."""

    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


@dataclass(frozen=True)
class TradeSignal:
    """예측 결과 기반 단일 종목 시그널."""

    code: str
    action: SignalAction
    expected_return: float
    up_probability: float
    confidence: float
    last_close: float
    target_price: float
    lower_price: float
    upper_price: float
    horizon: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "action": self.action.value,
            "expected_return": self.expected_return,
            "up_probability": self.up_probability,
            "confidence": self.confidence,
            "last_close": self.last_close,
            "target_price": self.target_price,
            "lower_price": self.lower_price,
            "upper_price": self.upper_price,
            "horizon": self.horizon,
            "reason": self.reason,
        }


def analyze_forecast(
    forecast: ForecastResult | Mapping[str, Any],
    *,
    buy_threshold: float = 0.02,
    sell_threshold: float = -0.02,
    min_up_probability: float = 0.55,
    max_up_probability_for_sell: float = 0.45,
) -> TradeSignal:
    """ForecastResult/Redis payload 로부터 BUY/HOLD/SELL 시그널 생성.

    기본 정책:
    - BUY: 최종 중앙값 기대수익률 >= buy_threshold 이고 상승확률 >= min_up_probability
    - SELL: 최종 중앙값 기대수익률 <= sell_threshold 이거나 상승확률 <= max_up_probability_for_sell
    - 그 외 HOLD

    `confidence` 는 기대수익률 크기와 상승확률의 0.5 대비 거리 중 큰 값을 0~1로 클램프한
    단순 설명용 점수다. 포지션 사이징은 여기서 결정하지 않는다.
    """
    payload = _coerce_forecast(forecast)
    last_close = payload["last_close"]
    target_price = payload["median_close"][-1]
    lower_price = payload["lower_close"][-1]
    upper_price = payload["upper_close"][-1]
    expected_return = target_price / last_close - 1.0 if last_close else 0.0
    up_probability = payload["up_probability"]

    if expected_return >= buy_threshold and up_probability >= min_up_probability:
        action = SignalAction.BUY
        reason = (
            f"expected_return {expected_return:.2%} >= {buy_threshold:.2%} "
            f"and up_probability {up_probability:.0%} >= {min_up_probability:.0%}"
        )
    elif expected_return <= sell_threshold or up_probability <= max_up_probability_for_sell:
        action = SignalAction.SELL
        reason = (
            f"expected_return {expected_return:.2%} <= {sell_threshold:.2%} "
            f"or up_probability {up_probability:.0%} <= {max_up_probability_for_sell:.0%}"
        )
    else:
        action = SignalAction.HOLD
        reason = (
            f"expected_return {expected_return:.2%}, up_probability {up_probability:.0%} "
            "within neutral band"
        )

    confidence = float(np.clip(max(abs(expected_return), abs(up_probability - 0.5) * 2), 0.0, 1.0))
    return TradeSignal(
        code=payload["code"],
        action=action,
        expected_return=float(expected_return),
        up_probability=float(up_probability),
        confidence=confidence,
        last_close=float(last_close),
        target_price=float(target_price),
        lower_price=float(lower_price),
        upper_price=float(upper_price),
        horizon=int(payload["horizon"]),
        reason=reason,
    )


def analyze_many(
    forecasts: Mapping[str, ForecastResult | Mapping[str, Any]],
    **kwargs: Any,
) -> dict[str, TradeSignal]:
    """여러 종목 예측 결과를 시그널 dict 로 변환."""
    return {code: analyze_forecast(forecast, **kwargs) for code, forecast in forecasts.items()}


def _coerce_forecast(forecast: ForecastResult | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(forecast, ForecastResult):
        return {
            "code": forecast.code,
            "last_close": float(forecast.last_close),
            "horizon": int(forecast.horizon),
            "median_close": _float_list(forecast.median_close),
            "lower_close": _float_list(forecast.lower_close),
            "upper_close": _float_list(forecast.upper_close),
            "up_probability": float(forecast.up_probability),
        }

    required = ["code", "last_close", "horizon", "median_close", "lower_close", "upper_close", "up_probability"]
    missing = [k for k in required if k not in forecast]
    if missing:
        raise ValueError(f"forecast payload missing keys: {missing}")
    return {
        "code": str(forecast["code"]),
        "last_close": float(forecast["last_close"]),
        "horizon": int(forecast["horizon"]),
        "median_close": _float_list(forecast["median_close"]),
        "lower_close": _float_list(forecast["lower_close"]),
        "upper_close": _float_list(forecast["upper_close"]),
        "up_probability": float(forecast["up_probability"]),
    }


def _float_list(values: Any) -> list[float]:
    out = [float(v) for v in values]
    if not out:
        raise ValueError("forecast price arrays must not be empty")
    return out
