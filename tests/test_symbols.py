"""tests/test_symbols.py — stock code display helpers."""

from common.symbols import display_symbol, symbol_name


def test_default_symbol_names_include_watchlist_additions():
    assert symbol_name("005380") == "현대차"
    assert symbol_name("001440") == "대한전선"
    assert symbol_name("006800") == "미래에셋증권"
    assert symbol_name("045100") == "한양이엔지"


def test_display_symbol_falls_back_to_code_for_unknown_symbol():
    assert display_symbol("005380") == "현대차 (005380)"
    assert display_symbol("999999") == "999999"
