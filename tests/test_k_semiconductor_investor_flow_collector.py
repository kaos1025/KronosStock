"""K-Semiconductor 국내 투자자 수급 collector 단위 테스트.

실제 KIS/KRX 네트워크는 호출하지 않고 fixture/가짜 client/임시 SQLite만 사용한다.
"""
from __future__ import annotations

import json
import stat
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from inference.k_semiconductor_investor_flow_collector import (
    InvestorFlowCollector,
    InvestorFlowRecord,
    InvestorFlowRepository,
    KisInvestorFlowClient,
    RateLimiter,
    StaleDataError,
)


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "001_k_semiconductor_flow_desk_permanent_ledgers.sql"
KST = timezone(timedelta(hours=9))


@pytest.fixture
def ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(MIGRATION.read_text(encoding="utf-8"))
    return conn


class FakeClient:
    def __init__(self):
        self.calls: list[str] = []

    def fetch_inquire_investor(self, symbol: str) -> list[dict[str, str]]:
        self.calls.append(f"inquire:{symbol}")
        return [
            {
                "stck_bsop_date": "20260731",
                "frgn_ntby_qty": "8359011",
                "frgn_ntby_tr_pbmn": "2099936",
                "orgn_ntby_qty": "3618959",
                "orgn_ntby_tr_pbmn": "935049",
            }
        ]

    def fetch_institution_daily(self, symbol: str, trade_date: date | None = None) -> list[dict[str, str]]:
        self.calls.append(f"institution:{symbol}:{trade_date.isoformat() if trade_date else 'latest'}")
        return [
            {
                "stck_bsop_date": "20260731",
                "fund_ntby_qty": "296441",
                "fund_ntby_tr_pbmn": "70860",
            }
        ]


class FlakyClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.failures_left = 1

    def fetch_inquire_investor(self, symbol: str) -> list[dict[str, str]]:
        self.calls.append(f"inquire:{symbol}")
        if self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("EGW00201 rate limit")
        return [
            {
                "stck_bsop_date": "20260731",
                "frgn_ntby_qty": "8359011",
                "frgn_ntby_tr_pbmn": "2099936",
                "orgn_ntby_qty": "3618959",
                "orgn_ntby_tr_pbmn": "935049",
            }
        ]


def test_kis_client_uses_only_readonly_quote_endpoints_and_paces_requests(monkeypatch):
    requests: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
    sleeps: list[float] = []

    class DummyResponse:
        status_code = 200
        headers = {}

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class DummyHttp:
        def post(self, path, data=None, json=None):  # noqa: A002
            requests.append(("POST", path, {}, data or json or {}))
            return DummyResponse({"access_token": "token", "expires_in": 86400, "token_type": "Bearer"})

        def get(self, path, *, headers, params):
            requests.append(("GET", path, dict(headers), dict(params)))
            return DummyResponse({"rt_cd": "0", "output": []})

    client = KisInvestorFlowClient(
        appkey="app",
        appsecret="secret",
        http_client=DummyHttp(),
        rate_limiter=RateLimiter(min_interval_seconds=1.2, sleep=sleeps.append, monotonic=lambda: 0.0),
        keep_token=False,
    )

    client.fetch_inquire_investor("005930")
    client.fetch_institution_daily("005930", trade_date=date(2026, 7, 31))

    methods_paths = [(method, path) for method, path, _, _ in requests]
    assert methods_paths == [
        ("POST", "/oauth2/tokenP"),
        ("GET", "/uapi/domestic-stock/v1/quotations/inquire-investor"),
        ("GET", "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"),
    ]
    assert all("order" not in path.lower() and "trading" not in path.lower() for _, path in methods_paths)
    assert requests[1][2]["tr_id"] == "FHKST01010900"
    assert requests[2][2]["tr_id"] == "FHPTJ04160001"
    assert requests[2][3]["FID_INPUT_DATE_1"] == "20260731"
    assert requests[2][3]["FID_ORG_ADJ_PRC"] == ""
    assert requests[2][3]["FID_ETC_CLS_CODE"] == ""
    assert sleeps == [1.2]


def test_kis_client_rejects_non_allowlisted_endpoint_before_http_call():
    class NoHttp:
        def get(self, *args, **kwargs):
            raise AssertionError("non-allowlisted endpoint must not reach HTTP")

    client = KisInvestorFlowClient(
        appkey="app",
        appsecret="secret",
        http_client=NoHttp(),
        rate_limiter=RateLimiter(min_interval_seconds=0),
        keep_token=False,
    )

    with pytest.raises(ValueError, match="not allowlisted"):
        client._get_readonly(  # noqa: SLF001 - verify the safety boundary directly
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id="TTTC0802U",
            params={},
        )


def test_kis_client_reuses_persisted_token_without_new_tokenp_call(tmp_path):
    requests: list[tuple[str, str]] = []
    token_path = tmp_path / "kis-token.json"
    token_path.write_text(
        json.dumps({"access_token": "cached-token", "expires_at": time.time() + 3600}),
        encoding="utf-8",
    )

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"rt_cd": "0", "output": []}

    class DummyHttp:
        def post(self, path, data=None, json=None):  # noqa: A002, ARG002
            requests.append(("POST", path))
            raise AssertionError("tokenP should not be called when a valid persisted token exists")

        def get(self, path, *, headers, params):  # noqa: ARG002
            requests.append(("GET", path))
            assert headers["authorization"] == "Bearer cached-token"
            return DummyResponse()

    client = KisInvestorFlowClient(
        appkey="app",
        appsecret="secret",
        http_client=DummyHttp(),
        rate_limiter=RateLimiter(min_interval_seconds=0, sleep=lambda _: None),
        token_cache_path=token_path,
    )

    client.fetch_inquire_investor("005930")

    assert requests == [("GET", "/uapi/domestic-stock/v1/quotations/inquire-investor")]


def test_kis_client_persists_new_token_for_later_reuse(tmp_path):
    requests: list[tuple[str, str]] = []
    token_path = tmp_path / "kis-token.json"

    class DummyResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class DummyHttp:
        def post(self, path, data=None, json=None):  # noqa: A002, ARG002
            requests.append(("POST", path))
            return DummyResponse({"access_token": "new-token", "expires_in": 3600})

        def get(self, path, *, headers, params):  # noqa: ARG002
            requests.append(("GET", path))
            assert headers["authorization"] == "Bearer new-token"
            return DummyResponse({"rt_cd": "0", "output": []})

    client = KisInvestorFlowClient(
        appkey="app",
        appsecret="secret",
        http_client=DummyHttp(),
        rate_limiter=RateLimiter(min_interval_seconds=0, sleep=lambda _: None),
        token_cache_path=token_path,
    )

    client.fetch_inquire_investor("005930")
    cached = json.loads(token_path.read_text(encoding="utf-8"))

    assert requests == [("POST", "/oauth2/tokenP"), ("GET", "/uapi/domestic-stock/v1/quotations/inquire-investor")]
    assert cached["access_token"] == "new-token"
    assert cached["expires_at"] > time.time()
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_collector_normalizes_foreign_institution_and_kis_fund_taxonomy():
    collector = InvestorFlowCollector(client=FakeClient(), stale_after_days=3, now=lambda: datetime(2026, 8, 1, 10, 0, tzinfo=KST))

    records = collector.collect_symbol("005930")

    assert [r.investor_category for r in records] == ["foreigner", "institution_total", "kis_fund"]
    assert [r.source_taxonomy for r in records] == ["KIS_FOREIGNER", "KIS_INSTITUTION_TOTAL", "KIS_FUND"]
    assert {r.source_status for r in records} == {"INTRADAY_ESTIMATE"}
    assert records[0].net_buy_quantity == 8359011
    assert records[0].net_buy_amount_million_krw == 2099936
    assert records[2].net_buy_quantity == 296441


def test_stale_data_is_blocked_when_latest_row_is_too_old():
    class OldClient(FakeClient):
        def fetch_inquire_investor(self, symbol: str) -> list[dict[str, str]]:
            row = super().fetch_inquire_investor(symbol)[0]
            row["stck_bsop_date"] = "20260720"
            return [row]

    collector = InvestorFlowCollector(client=OldClient(), stale_after_days=3, now=lambda: datetime(2026, 8, 1, 10, 0, tzinfo=KST))

    with pytest.raises(StaleDataError):
        collector.collect_symbol("005930")


def test_future_investor_trade_date_is_rejected_before_institution_request():
    class FutureClient(FakeClient):
        def fetch_inquire_investor(self, symbol: str) -> list[dict[str, str]]:
            row = super().fetch_inquire_investor(symbol)[0]
            row["stck_bsop_date"] = "20260802"
            return [row]

        def fetch_institution_daily(self, symbol: str, trade_date: date | None = None) -> list[dict[str, str]]:
            raise AssertionError("future-dated inquire row must fail before institution lookup")

    collector = InvestorFlowCollector(client=FutureClient(), stale_after_days=3, now=lambda: datetime(2026, 8, 1, 10, 0, tzinfo=KST))

    with pytest.raises(StaleDataError, match="future"):
        collector.collect_symbol("005930")


def test_missing_matching_fund_date_is_not_silently_attributed():
    class MismatchedInstitutionClient(FakeClient):
        def fetch_institution_daily(self, symbol: str, trade_date: date | None = None) -> list[dict[str, str]]:
            row = super().fetch_institution_daily(symbol, trade_date)[0]
            row["stck_bsop_date"] = "20260730"
            return [row]

    collector = InvestorFlowCollector(client=MismatchedInstitutionClient(), stale_after_days=3, now=lambda: datetime(2026, 8, 1, 10, 0, tzinfo=KST))

    with pytest.raises(RuntimeError, match="missing KIS fund row for 2026-07-31"):
        collector.collect_symbol("005930")


def test_today_row_is_marked_intraday_estimate_not_close_confirmed():
    # FakeClient rows are dated 2026-07-31 (a Friday XKRX session) == today.
    collector = InvestorFlowCollector(client=FakeClient(), stale_after_days=3, now=lambda: datetime(2026, 7, 31, 16, 10, tzinfo=KST))

    records = collector.collect_symbol("005930")

    assert {r.source_status for r in records} == {"INTRADAY_ESTIMATE"}


class OverridingClient(FakeClient):
    """inquire/institution 응답의 특정 필드를 테스트별로 덮어쓰는 fake."""

    def __init__(self, inquire_overrides=None, institution_overrides=None):
        super().__init__()
        self.inquire_overrides = inquire_overrides or {}
        self.institution_overrides = institution_overrides or {}

    def fetch_inquire_investor(self, symbol: str) -> list[dict[str, str]]:
        row = super().fetch_inquire_investor(symbol)[0]
        row.update(self.inquire_overrides)
        return [row]

    def fetch_institution_daily(self, symbol: str, trade_date: date | None = None) -> list[dict[str, str]]:
        row = super().fetch_institution_daily(symbol, trade_date)[0]
        row.update(self.institution_overrides)
        return [row]


@pytest.mark.parametrize("bad_value", [None, "", "None", "8,359,01x"], ids=["none", "empty", "none-string", "malformed"])
@pytest.mark.parametrize(
    "field,endpoint",
    [
        ("frgn_ntby_qty", "inquire"),
        ("orgn_ntby_qty", "inquire"),
        ("fund_ntby_qty", "institution"),
    ],
)
def test_required_quantity_missing_empty_or_malformed_fails_closed(field, endpoint, bad_value):
    from inference.k_semiconductor_investor_flow_collector import InvalidRequiredQuantityError

    overrides = {field: bad_value}
    client = OverridingClient(
        inquire_overrides=overrides if endpoint == "inquire" else None,
        institution_overrides=overrides if endpoint == "institution" else None,
    )
    collector = InvestorFlowCollector(client=client, stale_after_days=3, now=lambda: datetime(2026, 8, 1, 10, 0, tzinfo=KST))

    with pytest.raises(InvalidRequiredQuantityError, match=field):
        collector.collect_symbol("005930")


def test_required_quantity_literal_zero_remains_valid():
    client = OverridingClient(
        inquire_overrides={"frgn_ntby_qty": "0", "orgn_ntby_qty": "0"},
        institution_overrides={"fund_ntby_qty": "0"},
    )
    collector = InvestorFlowCollector(client=client, stale_after_days=3, now=lambda: datetime(2026, 8, 1, 10, 0, tzinfo=KST))

    records = collector.collect_symbol("005930")

    assert [r.net_buy_quantity for r in records] == [0, 0, 0]


def test_fabricated_weekend_trade_date_is_rejected():
    # 2026-08-01 is a Saturday: KIS must never legitimately report it as a session date.
    client = OverridingClient(
        inquire_overrides={"stck_bsop_date": "20260801"},
        institution_overrides={"stck_bsop_date": "20260801"},
    )
    collector = InvestorFlowCollector(client=client, stale_after_days=3, now=lambda: datetime(2026, 8, 2, 10, 0, tzinfo=KST))

    with pytest.raises(StaleDataError, match="not an XKRX session"):
        collector.collect_symbol("005930")


def test_prior_session_is_rejected_at_zero_lag_after_newer_completed_session():
    # Friday 2026-07-31 rows collected on Monday 2026-08-03 (a completed newer session).
    collector = InvestorFlowCollector(client=FakeClient(), stale_after_days=0, now=lambda: datetime(2026, 8, 3, 16, 10, tzinfo=KST))

    with pytest.raises(StaleDataError, match="stale"):
        collector.collect_symbol("005930")


def test_weekend_accepts_immediately_preceding_completed_session_at_zero_lag():
    collector = InvestorFlowCollector(client=FakeClient(), stale_after_days=0, now=lambda: datetime(2026, 8, 2, 9, 0, tzinfo=KST))

    records = collector.collect_symbol("005930")

    assert {r.trade_date for r in records} == {date(2026, 7, 31)}


def test_same_day_row_before_close_is_rejected_at_zero_lag():
    # 10:00 KST on Monday 2026-08-03: Monday's session has not completed (15:30 close) yet.
    client = OverridingClient(
        inquire_overrides={"stck_bsop_date": "20260803"},
        institution_overrides={"stck_bsop_date": "20260803"},
    )
    collector = InvestorFlowCollector(
        client=client, stale_after_days=0, now=lambda: datetime(2026, 8, 3, 10, 0, tzinfo=KST)
    )

    with pytest.raises(StaleDataError, match="not completed"):
        collector.collect_symbol("005930")


def test_prior_completed_session_is_accepted_before_close_at_zero_lag():
    # Friday 2026-07-31 is the latest completed session at Monday 10:00 KST.
    collector = InvestorFlowCollector(
        client=FakeClient(), stale_after_days=0, now=lambda: datetime(2026, 8, 3, 10, 0, tzinfo=KST)
    )

    records = collector.collect_symbol("005930")

    assert {r.trade_date for r in records} == {date(2026, 7, 31)}


def test_same_day_row_after_close_is_accepted_at_zero_lag():
    client = OverridingClient(
        inquire_overrides={"stck_bsop_date": "20260803"},
        institution_overrides={"stck_bsop_date": "20260803"},
    )
    collector = InvestorFlowCollector(
        client=client, stale_after_days=0, now=lambda: datetime(2026, 8, 3, 16, 10, tzinfo=KST)
    )

    records = collector.collect_symbol("005930")

    assert {r.trade_date for r in records} == {date(2026, 8, 3)}


def test_calendar_unavailable_fails_closed_without_weekday_fallback(monkeypatch):
    from inference import k_semiconductor_investor_flow_collector as module

    def broken_calendar():
        raise RuntimeError("xkrx unavailable")

    monkeypatch.setattr(module, "_get_xkrx_calendar", broken_calendar)
    # Row is fresh (same day) — only the calendar failure can cause a rejection here.
    collector = InvestorFlowCollector(client=FakeClient(), stale_after_days=3, now=lambda: datetime(2026, 7, 31, 16, 10, tzinfo=KST))

    with pytest.raises(module.XkrxCalendarUnavailableError):
        collector.collect_symbol("005930")


def test_retry_wraps_transient_rate_limit_failures():
    client = FlakyClient()
    collector = InvestorFlowCollector(client=client, stale_after_days=3, now=lambda: datetime(2026, 8, 1, 10, 0, tzinfo=KST), max_attempts=2, backoff_seconds=0, sleep=lambda _: None)

    records = collector.collect_symbol("005930")

    assert len(records) == 3
    assert client.calls.count("inquire:005930") == 2


def test_repository_stores_each_symbol_independently_and_is_idempotent(ledger):
    repo = InvestorFlowRepository(ledger)
    records = [
        InvestorFlowRecord(
            symbol="005930",
            trade_date=date(2026, 7, 31),
            investor_category="foreigner",
            source_taxonomy="KIS_FOREIGNER",
            net_buy_quantity=8359011,
            net_buy_amount_million_krw=2099936,
            source_status="INTRADAY_ESTIMATE",
            source_name="KIS_INQUIRE_INVESTOR",
        ),
        InvestorFlowRecord(
            symbol="000660",
            trade_date=date(2026, 7, 31),
            investor_category="foreigner",
            source_taxonomy="KIS_FOREIGNER",
            net_buy_quantity=2205767,
            net_buy_amount_million_krw=3648525,
            source_status="INTRADAY_ESTIMATE",
            source_name="KIS_INQUIRE_INVESTOR",
        ),
    ]

    first = repo.store_records(records, as_of_kst="2026-08-01T09:00:00+09:00")
    second = repo.store_records(records, as_of_kst="2026-08-01T09:00:00+09:00")

    assert first.inserted_features == 2
    assert second.inserted_features == 0
    rows = ledger.execute(
        "SELECT symbol, feature_name, value_num FROM ksf_normalized_features ORDER BY symbol"
    ).fetchall()
    assert [(r["symbol"], r["feature_name"], r["value_num"]) for r in rows] == [
        ("000660", "foreigner_net_buy_quantity", 2205767.0),
        ("005930", "foreigner_net_buy_quantity", 8359011.0),
    ]
    run_symbols = [r[0] for r in ledger.execute("SELECT symbol FROM ksf_runs ORDER BY symbol").fetchall()]
    assert run_symbols == ["000660", "005930"]
    assert ledger.execute(
        """
        SELECT COUNT(*) FROM ksf_source_snapshots s
        JOIN ksf_collected_source_metadata m
          ON m.metadata_id = s.source_metadata_id AND m.source_name = s.source_name
        """
    ).fetchone()[0] == 2


def test_repository_mid_batch_integrity_failure_rolls_back_and_propagates(ledger):
    ledger.execute(
        """CREATE TRIGGER reject_second_symbol BEFORE INSERT ON ksf_source_snapshots
           WHEN NEW.symbol = '000660' BEGIN SELECT RAISE(ABORT, 'second symbol rejected'); END"""
    )
    records = [
        InvestorFlowRecord(symbol=symbol, trade_date=date(2026, 7, 31), investor_category="foreigner",
                           source_taxonomy="KIS_FOREIGNER", net_buy_quantity=1,
                           net_buy_amount_million_krw=1, source_status="INTRADAY_ESTIMATE",
                           source_name="KIS_INQUIRE_INVESTOR")
        for symbol in ("005930", "000660")
    ]

    with pytest.raises(sqlite3.IntegrityError, match="second symbol rejected"):
        InvestorFlowRepository(ledger).store_records(records, as_of_kst="2026-08-01T09:00:00+09:00")

    assert ledger.execute("SELECT COUNT(*) FROM ksf_runs").fetchone()[0] == 0
    assert ledger.execute("SELECT COUNT(*) FROM ksf_source_snapshots").fetchone()[0] == 0
    assert ledger.execute("SELECT COUNT(*) FROM ksf_normalized_features").fetchone()[0] == 0


def test_different_observation_time_creates_separate_run_for_revised_payload(ledger):
    repo = InvestorFlowRepository(ledger)
    original = InvestorFlowRecord(
        symbol="005930",
        trade_date=date(2026, 7, 31),
        investor_category="foreigner",
        source_taxonomy="KIS_FOREIGNER",
        net_buy_quantity=8359011,
        net_buy_amount_million_krw=2099936,
        source_status="INTRADAY_ESTIMATE",
        source_name="KIS_INQUIRE_INVESTOR",
    )
    revised = InvestorFlowRecord(
        symbol="005930",
        trade_date=date(2026, 7, 31),
        investor_category="foreigner",
        source_taxonomy="KIS_FOREIGNER",
        net_buy_quantity=8359012,
        net_buy_amount_million_krw=2099937,
        source_status="INTRADAY_ESTIMATE",
        source_name="KIS_INQUIRE_INVESTOR",
    )

    repo.store_records([original], as_of_kst="2026-08-01T09:00:00+09:00")
    result = repo.store_records([revised], as_of_kst="2026-08-01T09:05:00+09:00")

    assert result.inserted_runs == 1
    assert result.inserted_features == 1
    rows = ledger.execute(
        "SELECT value_num, feature_version FROM ksf_normalized_features ORDER BY value_num"
    ).fetchall()
    assert [r["value_num"] for r in rows] == [8359011.0, 8359012.0]
    assert len({r["feature_version"] for r in rows}) == 2
    runs = ledger.execute(
        "SELECT as_of_kst, available_data_cutoff FROM ksf_runs ORDER BY as_of_kst"
    ).fetchall()
    assert [tuple(row) for row in runs] == [
        ("2026-08-01T09:00:00+09:00", "2026-08-01T09:00:00+09:00"),
        ("2026-08-01T09:05:00+09:00", "2026-08-01T09:05:00+09:00"),
    ]


def test_repository_uses_true_observation_time_for_prior_day_kis_row(ledger):
    record = InvestorFlowRecord(
        symbol="005930",
        trade_date=date(2026, 7, 31),
        investor_category="kis_fund",
        source_taxonomy="KIS_FUND",
        net_buy_quantity=123,
        net_buy_amount_million_krw=4,
        source_status="INTRADAY_ESTIMATE",
        source_name="KIS_INVESTOR_TRADE_BY_STOCK_DAILY",
    )

    result = InvestorFlowRepository(ledger).store_records(
        [record], as_of_kst="2026-08-01T10:15:00+09:00"
    )

    assert result.inserted_features == 1
    row = ledger.execute(
        "SELECT source_as_of, ingested_at_kst, available_data_cutoff FROM ksf_source_snapshots"
    ).fetchone()
    assert tuple(row) == (
        "2026-08-01T10:15:00+09:00",
        "2026-08-01T10:15:00+09:00",
        "2026-08-01T10:15:00+09:00",
    )
