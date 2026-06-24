"""bot/alert_bot.py — Telegram 알림 메시지 포맷터와 선택적 전송 래퍼.

테스트 가능한 핵심은 순수 문자열 포맷터다. 실제 Telegram 전송은 `send_text`에 격리되어 있으며,
토큰/채팅 ID가 없으면 명시적으로 실패한다. 실전 주문과는 무관한 알림 계층이다.
"""
from __future__ import annotations

import logging
from typing import Iterable

from common.config import settings
from common.symbols import display_symbol
from strategy.analyzer import SignalAction, TradeSignal
from strategy.paper_trader import PaperOrder, PaperPortfolio

# httpx 가 요청 URL 을 INFO 로 로깅하면 Telegram sendMessage URL(봇 토큰 포함)이 평문으로
# journald 등 로그에 남는다(토큰 누출). telegram HTTP 클라이언트(httpx)의 로거를 WARNING 으로
# 낮춰 누출을 차단한다. 이 모듈을 import 하는 모든 전송 경로(프로덕션 dry-run 포함)에 적용된다.
logging.getLogger("httpx").setLevel(logging.WARNING)


def format_signal(signal: TradeSignal) -> str:
    """단일 종목 시그널을 Telegram markdown-friendly 텍스트로 변환."""
    emoji = {
        SignalAction.BUY: "🟢",
        SignalAction.HOLD: "⚪",
        SignalAction.SELL: "🔴",
    }[signal.action]
    return (
        f"{emoji} *{display_symbol(signal.code)}* `{signal.action.value}`\n"
        f"예상수익률: {signal.expected_return:+.2%}\n"
        f"상승확률: {signal.up_probability:.0%}\n"
        f"목표가: {signal.target_price:,.0f} / 현재가: {signal.last_close:,.0f}\n"
        f"밴드: {signal.lower_price:,.0f} ~ {signal.upper_price:,.0f}\n"
        f"근거: {signal.reason}"
    )


def format_signal_digest(signals: Iterable[TradeSignal]) -> str:
    """여러 시그널을 한 번에 보낼 digest 메시지로 포맷."""
    items = list(signals)
    if not items:
        return "📭 KronosStock: 생성된 시그널이 없습니다."
    body = "\n\n".join(format_signal(signal) for signal in items)
    return f"📈 *KronosStock 시그널*\n\n{body}"


def format_orders(orders: Iterable[PaperOrder], portfolio: PaperPortfolio | None = None) -> str:
    """paper order 결과를 알림 텍스트로 변환."""
    items = list(orders)
    if not items:
        return "🧾 Paper trading: 체결된 주문이 없습니다."
    lines = ["🧾 *Paper trading 체결*\n"]
    for order in items:
        lines.append(
            f"- `{order.side.value}` {display_symbol(order.code)} x {order.quantity:,} "
            f"@ {order.price:,.0f} = {order.notional:,.0f}"
        )
    if portfolio is not None:
        lines.append(f"\n현금: {portfolio.cash:,.0f}")
        lines.append(f"보유: {portfolio.positions}")
    return "\n".join(lines)


async def send_text(text: str) -> None:
    """Telegram Bot API 전송. 구성 없으면 RuntimeError.

    단위 테스트/스케줄 dry-run은 이 함수를 호출하지 않고 formatter만 검증한다.
    """
    if not settings.telegram_configured:
        raise RuntimeError("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 가 설정되지 않았습니다.")
    from telegram import Bot

    bot = Bot(settings.telegram_bot_token)
    await bot.send_message(chat_id=settings.telegram_chat_id, text=text, parse_mode="Markdown")
