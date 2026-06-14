"""bot/scheduler.py — KronosStock dry-run 자동화 job.

Phase 3의 안전한 자동화 시작점이다. 기본 job은 다음 순서만 수행한다.

1. forecast 함수 호출(기본: `run_watchlist_forecast`, 테스트에서는 주입 가능)
2. ForecastResult/Redis payload → TradeSignal 변환
3. in-memory/Redis paper portfolio 에만 paper order 적용
4. Telegram-friendly digest 문자열 생성
5. `send_alert=True`일 때만 Telegram 전송

실제 KIS 주문 endpoint 또는 broker API는 호출하지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from apscheduler.schedulers.background import BackgroundScheduler

from bot.alert_bot import format_orders, format_signal_digest, send_text
from common.config import settings
from common.redis_client import get_redis, key
from inference.forecast_runner import run_watchlist_forecast
from inference.predictor import ForecastResult
from strategy.analyzer import TradeSignal, analyze_many
from strategy.paper_trader import PaperOrder, PaperPortfolio, apply_signals

logger = logging.getLogger(__name__)

PAPER_PORTFOLIO_KEY = key("paper", "portfolio")


@dataclass(frozen=True)
class DryRunResult:
    """dry-run 1회 실행 결과."""

    signals: dict[str, TradeSignal]
    orders: list[PaperOrder]
    portfolio: PaperPortfolio
    message: str
    portfolio_key: str | None = None
    valuation_prices: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        prices = self.valuation_prices or _prices_from_signals(self.signals)
        return {
            "signals": {code: signal.as_dict() for code, signal in self.signals.items()},
            "orders": [order.__dict__ for order in self.orders],
            "portfolio": self.portfolio.snapshot(prices),
            "message": self.message,
            "portfolio_key": self.portfolio_key,
        }


def run_dry_run_cycle(
    *,
    forecasts: Mapping[str, ForecastResult | Mapping[str, Any]] | None = None,
    forecast_func: Callable[[], Mapping[str, ForecastResult | Mapping[str, Any]]] | None = None,
    portfolio: PaperPortfolio | None = None,
    prices: Mapping[str, float] | None = None,
    persist_portfolio: bool = True,
    send_alert: bool = False,
    max_position_pct: float = 0.2,
    min_order_cash: float = 10_000.0,
) -> DryRunResult:
    """watchlist forecast → signal → paper order → digest 를 1회 실행.

    Args:
        forecasts: 테스트/수동 dry-run 용 사전 계산 forecast. 주어지면 `forecast_func`를 호출하지 않는다.
        forecast_func: forecast 생성 함수. 기본은 `run_watchlist_forecast`.
        portfolio: 시작 paper portfolio. None이면 Redis snapshot을 읽고, 없으면 기본 현금 1,000,000.
        prices: 기존 보유 종목까지 평가하기 위한 가격 맵. signal 종목 가격은 last_close로 자동 보강한다.
        persist_portfolio: True면 paper portfolio snapshot을 Redis에 저장한다.
        send_alert: True면 Telegram 전송까지 수행한다. 기본 False라 테스트/cron dry-run이 안전하다.

    Returns:
        DryRunResult. 실제 주문/API 호출은 없다.
    """
    if forecasts is None:
        producer = forecast_func or (lambda: run_watchlist_forecast(horizon=settings.kronos_pred_len, n_paths=settings.forecast_n_paths))
        forecasts = producer()

    signals = analyze_many(forecasts)
    paper_portfolio = portfolio or load_paper_portfolio(default=PaperPortfolio())
    price_map = {**dict(prices or {}), **_prices_from_signals(signals)}
    orders = apply_signals(
        paper_portfolio,
        signals,
        prices=price_map,
        max_position_pct=max_position_pct,
        min_order_cash=min_order_cash,
    )

    portfolio_key: str | None = None
    if persist_portfolio:
        portfolio_key = save_paper_portfolio(paper_portfolio)

    message = format_signal_digest(signals.values()) + "\n\n" + format_orders(orders, paper_portfolio)
    if send_alert:
        asyncio.run(send_text(message))

    logger.info("dry-run 완료: signals=%d orders=%d", len(signals), len(orders))
    return DryRunResult(
        signals=signals,
        orders=orders,
        portfolio=paper_portfolio,
        message=message,
        portfolio_key=portfolio_key,
        valuation_prices=price_map,
    )


def create_scheduler(*, send_alert: bool = False) -> BackgroundScheduler:
    """APScheduler BackgroundScheduler 구성.

    CLI/서비스에서 `scheduler.start()`로 실행한다. 테스트는 이 함수가 job 등록만 하는지 검증한다.
    """
    scheduler = BackgroundScheduler(timezone=settings.timezone or "Asia/Seoul")
    job_times = [
        ("premarket", settings.schedule_premarket),
        ("open", settings.schedule_open),
        ("midday", settings.schedule_midday),
        ("close", settings.schedule_close),
    ]
    for name, hhmm in job_times:
        hour, minute = _parse_hhmm(hhmm)
        scheduler.add_job(
            run_dry_run_cycle,
            trigger="cron",
            hour=hour,
            minute=minute,
            id=f"kronos-dry-run-{name}",
            name=f"KronosStock dry-run {name}",
            kwargs={"send_alert": send_alert},
            replace_existing=True,
        )
    return scheduler


def load_paper_portfolio(*, default: PaperPortfolio | None = None) -> PaperPortfolio:
    """Redis snapshot에서 paper portfolio 복원. 없거나 실패하면 default 반환."""
    fallback = default or PaperPortfolio()
    try:
        raw = get_redis().get(PAPER_PORTFOLIO_KEY)
    except Exception as exc:  # noqa: BLE001 - Redis 없어도 dry-run은 시작 가능해야 한다
        logger.warning("paper portfolio load skipped: %s", exc)
        return fallback
    if not raw:
        return fallback
    try:
        payload = json.loads(raw)
        return PaperPortfolio(
            cash=float(payload.get("cash", fallback.cash)),
            positions={str(k): int(v) for k, v in payload.get("positions", {}).items()},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("paper portfolio snapshot invalid; using default: %s", exc)
        return fallback


def save_paper_portfolio(portfolio: PaperPortfolio) -> str:
    """paper portfolio snapshot을 Redis에 저장."""
    payload = {
        "cash": float(portfolio.cash),
        "positions": dict(portfolio.positions),
        "orders": [order.__dict__ for order in portfolio.orders[-50:]],
    }
    get_redis().set(PAPER_PORTFOLIO_KEY, json.dumps(payload, ensure_ascii=False))
    return PAPER_PORTFOLIO_KEY


def _prices_from_signals(signals: Mapping[str, TradeSignal]) -> dict[str, float]:
    return {code: float(signal.last_close) for code, signal in signals.items()}


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour_s, minute_s = value.split(":", 1)
        hour, minute = int(hour_s), int(minute_s)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid HH:MM schedule value: {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HH:MM schedule value: {value!r}")
    return hour, minute


if __name__ == "__main__":
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    result = run_dry_run_cycle(send_alert=False)
    print(result.message)
    if result.portfolio_key:
        print("portfolio ->", result.portfolio_key)
