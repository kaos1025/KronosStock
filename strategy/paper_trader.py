"""strategy/paper_trader.py — broker API 없는 모의투자 포트폴리오 엔진.

Phase 3 자동화의 안전한 첫 단계다. 이 모듈은 주문 API/KIS/Redis/네트워크를 호출하지 않고,
`TradeSignal` 과 현재가를 받아 in-memory 포트폴리오만 갱신한다. 실전/모의 broker 어댑터는
나중에 별도 계층으로 붙이고, 여기서는 주문 의도와 리스크 제한을 검증한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from strategy.analyzer import SignalAction, TradeSignal


@dataclass(frozen=True)
class PaperOrder:
    """체결 완료로 가정한 paper order 기록."""

    code: str
    side: SignalAction
    quantity: int
    price: float
    notional: float
    reason: str
    created_at: str


@dataclass
class PaperPortfolio:
    """현금 + 정수 주식 수량 기반 단순 paper portfolio."""

    cash: float = 1_000_000.0
    positions: dict[str, int] = field(default_factory=dict)
    orders: list[PaperOrder] = field(default_factory=list)

    def market_value(self, prices: Mapping[str, float]) -> float:
        """주어진 현재가 기준 총 평가액."""
        stock_value = sum(qty * float(prices.get(code, 0.0)) for code, qty in self.positions.items())
        return float(self.cash + stock_value)

    def position_qty(self, code: str) -> int:
        return int(self.positions.get(code, 0))

    def snapshot(self, prices: Mapping[str, float] | None = None) -> dict[str, Any]:
        prices = prices or {}
        return {
            "cash": float(self.cash),
            "positions": dict(self.positions),
            "orders": [order.__dict__ for order in self.orders],
            "market_value": self.market_value(prices),
        }


def apply_signal(
    portfolio: PaperPortfolio,
    signal: TradeSignal,
    *,
    price: float | None = None,
    prices: Mapping[str, float] | None = None,
    max_position_pct: float = 0.2,
    min_order_cash: float = 10_000.0,
    now: datetime | None = None,
) -> PaperOrder | None:
    """시그널을 paper portfolio 에 적용하고 체결 기록을 반환.

    정책:
    - BUY: 현재 평가액의 `max_position_pct`까지 해당 종목 목표 노출을 맞춘다.
    - SELL: 보유 수량 전량을 매도한다.
    - HOLD: 아무 것도 하지 않는다.

    `price` 기본값은 signal.last_close. `prices` 는 기존 보유 종목까지 포함한 전체 평가용
    가격 맵이며, 없으면 signal 종목만 현재가로 평가한다. 수수료/슬리피지는 아직 반영하지
    않는 보수적 skeleton이며, 실전 주문으로 절대 연결되지 않는다.
    """
    valuation_prices = dict(prices or {})
    exec_price = float(price if price is not None else valuation_prices.get(signal.code, signal.last_close))
    valuation_prices[signal.code] = exec_price
    if exec_price <= 0.0:
        raise ValueError(f"price must be positive: {exec_price}")
    if not 0.0 < max_position_pct <= 1.0:
        raise ValueError(f"max_position_pct must be in (0, 1]: {max_position_pct}")
    if min_order_cash < 0.0:
        raise ValueError(f"min_order_cash must be >= 0: {min_order_cash}")

    if signal.action == SignalAction.HOLD:
        return None

    timestamp = (now or datetime.now().astimezone()).isoformat()
    current_qty = portfolio.position_qty(signal.code)

    if signal.action == SignalAction.SELL:
        if current_qty <= 0:
            return None
        notional = current_qty * exec_price
        portfolio.cash += notional
        portfolio.positions.pop(signal.code, None)
        order = PaperOrder(
            code=signal.code,
            side=SignalAction.SELL,
            quantity=current_qty,
            price=exec_price,
            notional=notional,
            reason=signal.reason,
            created_at=timestamp,
        )
        portfolio.orders.append(order)
        return order

    # BUY
    total_value = portfolio.market_value(valuation_prices)
    target_notional = total_value * max_position_pct
    current_notional = current_qty * exec_price
    additional_notional = max(0.0, min(target_notional - current_notional, portfolio.cash))
    if additional_notional < min_order_cash:
        return None
    quantity = int(additional_notional // exec_price)
    if quantity <= 0:
        return None

    notional = quantity * exec_price
    portfolio.cash -= notional
    portfolio.positions[signal.code] = current_qty + quantity
    order = PaperOrder(
        code=signal.code,
        side=SignalAction.BUY,
        quantity=quantity,
        price=exec_price,
        notional=notional,
        reason=signal.reason,
        created_at=timestamp,
    )
    portfolio.orders.append(order)
    return order


def apply_signals(
    portfolio: PaperPortfolio,
    signals: Mapping[str, TradeSignal],
    *,
    prices: Mapping[str, float] | None = None,
    max_position_pct: float = 0.2,
    min_order_cash: float = 10_000.0,
) -> list[PaperOrder]:
    """여러 종목 시그널을 순차 적용. HOLD/미체결은 결과에서 제외."""
    prices = prices or {}
    orders: list[PaperOrder] = []
    for code, signal in signals.items():
        order = apply_signal(
            portfolio,
            signal,
            price=float(prices.get(code, signal.last_close)),
            prices=prices,
            max_position_pct=max_position_pct,
            min_order_cash=min_order_cash,
        )
        if order is not None:
            orders.append(order)
    return orders
