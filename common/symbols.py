"""Stock symbol display helpers.

Keep a small default map for the current KronosStock watchlist so alerts and
reports are readable without introducing a live metadata dependency.
"""
from __future__ import annotations

DEFAULT_SYMBOL_NAMES: dict[str, str] = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "035420": "NAVER",
    "051910": "LG화학",
    "006400": "삼성SDI",
    "005380": "현대차",
    "001440": "대한전선",
    "006800": "미래에셋증권",
    "045100": "한양이엔지",
}


def symbol_name(code: str) -> str:
    """Return a human-readable company name for a stock code, or an empty string."""
    return DEFAULT_SYMBOL_NAMES.get(str(code), "")


def display_symbol(code: str) -> str:
    """Return `회사명 (코드)` when known, otherwise just the code."""
    name = symbol_name(code)
    return f"{name} ({code})" if name else str(code)
