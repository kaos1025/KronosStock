from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from inference.k_semiconductor_investor_flow_collector import KisInvestorFlowClient, RateLimiter

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "001_k_semiconductor_flow_desk_permanent_ledgers.sql"
KST = timezone(timedelta(hours=9))
CUTOFF = "2026-07-31T16:10:00+09:00"


@pytest.fixture
def ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(MIGRATION.read_text(encoding="utf-8"))
    return conn


def test_kis_client_fetches_daily_itemchartprice_with_exact_readonly_shape():
    requests: list[tuple[str, str, dict[str, str], dict[str, str]]] = []

    class DummyResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class DummyHttp:
        def post(self, path, data=None, json=None):  # noqa: A002
            requests.append(("POST", path, {}, data or json or {}))
            return DummyResponse({"access_token": "token", "expires_in": 86400})

        def get(self, path, *, headers, params):
            requests.append(("GET", path, dict(headers), dict(params)))
            return DummyResponse({"rt_cd": "0", "output2": [{"stck_bsop_date": "20260731"}]})

    client = KisInvestorFlowClient(
        appkey="app",
        appsecret="secret",
        http_client=DummyHttp(),
        rate_limiter=RateLimiter(min_interval_seconds=0, sleep=lambda _: None),
        keep_token=False,
    )

    rows = client.fetch_daily_itemchartprice("005930", start_date="20260701", end_date="20260731")

    assert rows == [{"stck_bsop_date": "20260731"}]
    assert [(method, path) for method, path, _, _ in requests] == [
        ("POST", "/oauth2/tokenP"),
        ("GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"),
    ]
    headers = requests[1][2]
    params = requests[1][3]
    assert headers["tr_id"] == "FHKST03010100"
    assert params == {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": "005930",
        "FID_INPUT_DATE_1": "20260701",
        "FID_INPUT_DATE_2": "20260731",
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    assert all("order" not in path.lower() and "trading" not in path.lower() for _, path, _, _ in requests)


class FakePriceClient:
    def __init__(self, rows_by_symbol):
        self.rows_by_symbol = rows_by_symbol
        self.calls: list[tuple[str, str, str]] = []

    def fetch_daily_itemchartprice(self, symbol: str, *, start_date: str, end_date: str):
        self.calls.append((symbol, start_date, end_date))
        return list(self.rows_by_symbol[symbol])


def test_price_collector_selects_latest_valid_row_before_cutoff_and_computes_return():
    from inference.k_semiconductor_domestic_price_collector import DomesticPriceCollector

    client = FakePriceClient(
        {
            "005930": [
                {"stck_bsop_date": "20260803", "stck_clpr": "90000", "acml_vol": "1"},
                {"stck_bsop_date": "20260731", "stck_clpr": "80,000", "acml_vol": "12,345,678"},
                {"stck_bsop_date": "20260730", "stck_clpr": "78,400", "acml_vol": "10"},
            ]
        }
    )

    record = DomesticPriceCollector(client=client).collect_symbol(
        "005930", available_data_cutoff=CUTOFF
    )

    assert record.symbol == "005930"
    assert record.trade_date.isoformat() == "2026-07-31"
    assert record.close == 80000.0
    assert record.volume == 12345678
    assert record.source_as_of == "2026-07-31T15:30:00+09:00"
    assert record.source_status == "INTRADAY_ESTIMATE"
    assert round(record.one_day_return_bps or 0, 2) == 204.08
    assert client.calls == [("005930", "20260701", "20260731")]


@pytest.mark.parametrize(
    "bad_row,match",
    [
        ({"stck_bsop_date": "20260731", "stck_clpr": "0", "acml_vol": "1"}, "no valid"),
        ({"stck_bsop_date": "20260731", "stck_clpr": "80000", "acml_vol": "0"}, "no valid"),
        ({"stck_bsop_date": "20260731", "stck_clpr": "bad", "acml_vol": "1"}, "no valid"),
        ({"stck_bsop_date": "20260731", "stck_clpr": "80000", "acml_vol": "bad"}, "no valid"),
        ({"stck_bsop_date": "20260803", "stck_clpr": "80000", "acml_vol": "1"}, "no valid"),
        ({"stck_bsop_date": "20260731", "stck_shrn_iscd": "000660", "stck_clpr": "80000", "acml_vol": "1"}, "no valid"),
    ],
)
def test_price_collector_rejects_empty_nonpositive_malformed_and_future_rows(bad_row, match):
    from inference.k_semiconductor_domestic_price_collector import DomesticPriceCollector

    with pytest.raises(RuntimeError, match=match):
        DomesticPriceCollector(client=FakePriceClient({"005930": [bad_row]})).collect_symbol(
            "005930", available_data_cutoff=CUTOFF
        )


def test_price_collector_is_exact_symbol_allowlist_only():
    from inference.k_semiconductor_domestic_price_collector import DomesticPriceCollector

    with pytest.raises(ValueError, match="unsupported symbol"):
        DomesticPriceCollector(client=FakePriceClient({})).collect_symbol(
            "035420", available_data_cutoff=CUTOFF
        )


def test_price_collector_fails_closed_when_xkrx_calendar_unavailable(monkeypatch):
    from inference import k_semiconductor_domestic_price_collector as module

    def no_calendar():
        raise RuntimeError("xkrx unavailable")

    monkeypatch.setattr(module, "_get_xkrx_calendar", no_calendar)
    # Row is fresh (cutoff-day session): any acceptance would prove a silent weekday fallback.
    client = FakePriceClient(
        {"005930": [{"stck_bsop_date": "20260731", "stck_clpr": "80000", "acml_vol": "1"}]}
    )

    with pytest.raises(module.XkrxCalendarUnavailableError):
        module.DomesticPriceCollector(client=client, stale_after_sessions=3).collect_symbol(
            "005930", available_data_cutoff=CUTOFF
        )


def test_price_collector_rejects_fabricated_weekend_response_date(monkeypatch):
    from inference import k_semiconductor_domestic_price_collector as module

    monkeypatch.setattr(
        module,
        "_get_xkrx_calendar",
        lambda: FakeXkrxCalendar({"2026-08-02": ["2026-07-31"]}),
    )
    # 2026-08-01 is a Saturday — not an XKRX session, even though it is before the cutoff.
    client = FakePriceClient(
        {"005930": [{"stck_bsop_date": "20260801", "stck_clpr": "80000", "acml_vol": "1"}]}
    )

    with pytest.raises(module.StaleDomesticPriceError, match="not an XKRX session"):
        module.DomesticPriceCollector(client=client).collect_symbol(
            "005930", available_data_cutoff="2026-08-02T09:00:00+09:00"
        )


class FakeXkrxCalendar:
    def __init__(self, sessions_by_end: dict[str, list[str]]):
        self.sessions_by_end = sessions_by_end

    def sessions_in_range(self, start: str, end: str):
        return self.sessions_by_end[end]


def test_price_collector_default_accepts_prior_completed_session_on_weekend(monkeypatch):
    from inference import k_semiconductor_domestic_price_collector as module

    monkeypatch.setattr(
        module,
        "_get_xkrx_calendar",
        lambda: FakeXkrxCalendar({"2026-08-02": ["2026-07-31"]}),
    )
    client = FakePriceClient(
        {"005930": [{"stck_bsop_date": "20260731", "stck_clpr": "80000", "acml_vol": "1"}]}
    )

    record = module.DomesticPriceCollector(client=client).collect_symbol(
        "005930", available_data_cutoff="2026-08-02T09:00:00+09:00"
    )

    assert record.trade_date.isoformat() == "2026-07-31"


def test_price_collector_default_rejects_previous_session_after_new_xkrx_session(monkeypatch):
    from inference import k_semiconductor_domestic_price_collector as module

    monkeypatch.setattr(
        module,
        "_get_xkrx_calendar",
        lambda: FakeXkrxCalendar({"2026-08-03": ["2026-07-31", "2026-08-03"]}),
    )
    client = FakePriceClient(
        {"005930": [{"stck_bsop_date": "20260731", "stck_clpr": "80000", "acml_vol": "1"}]}
    )

    with pytest.raises(module.StaleDomesticPriceError, match="latest=2026-07-31"):
        module.DomesticPriceCollector(client=client).collect_symbol(
            "005930", available_data_cutoff="2026-08-03T16:10:00+09:00"
        )


def test_price_repository_persists_provenance_features_and_idempotent_rerun(ledger):
    from inference.k_semiconductor_domestic_price_collector import DomesticPriceCollector, DomesticPriceRepository

    rows = {
        "005930": [
            {"stck_bsop_date": "20260731", "stck_clpr": "80000", "acml_vol": "12345678"},
            {"stck_bsop_date": "20260730", "stck_clpr": "78400", "acml_vol": "10"},
        ],
        "000660": [
            {"stck_bsop_date": "20260731", "stck_clpr": "210000", "acml_vol": "9876543"},
            {"stck_bsop_date": "20260730", "stck_clpr": "205000", "acml_vol": "10"},
        ],
    }
    records = DomesticPriceCollector(client=FakePriceClient(rows)).collect_symbols(
        ["005930", "000660"], available_data_cutoff=CUTOFF
    )
    repo = DomesticPriceRepository(ledger)

    first = repo.store_records(records, as_of_kst=CUTOFF, available_data_cutoff=CUTOFF)
    second = repo.store_records(records, as_of_kst=CUTOFF, available_data_cutoff=CUTOFF)

    assert first.inserted_features == 6
    assert second.inserted_features == 0
    features = ledger.execute(
        "SELECT symbol, feature_group, feature_name, value_num, unit FROM ksf_normalized_features ORDER BY symbol, feature_name"
    ).fetchall()
    assert [(r["symbol"], r["feature_group"], r["feature_name"], r["unit"]) for r in features] == [
        ("000660", "domestic_price", "close", "KRW"),
        ("000660", "domestic_price", "one_day_return_bps", "bps"),
        ("000660", "domestic_price", "volume", "shares"),
        ("005930", "domestic_price", "close", "KRW"),
        ("005930", "domestic_price", "one_day_return_bps", "bps"),
        ("005930", "domestic_price", "volume", "shares"),
    ]
    assert ledger.execute("PRAGMA foreign_key_check").fetchall() == []
    meta = ledger.execute("SELECT source_url, raw_storage_policy FROM ksf_collected_source_metadata").fetchone()
    assert "?" not in meta["source_url"]
    assert meta["raw_storage_policy"] == "METADATA_ONLY"
    snapshot = ledger.execute("SELECT normalized_payload_sha256, snapshot_metadata_json FROM ksf_source_snapshots").fetchone()
    assert len(snapshot["normalized_payload_sha256"]) == 64
    metadata_json = snapshot["snapshot_metadata_json"].lower()
    assert "stck_clpr" not in metadata_json
    assert "raw_payload" not in metadata_json
    assert "http" not in metadata_json


def test_price_repository_rolls_back_on_lineage_constraint_error(ledger):
    from inference.k_semiconductor_domestic_price_collector import DomesticPriceCollector, DomesticPriceRepository

    ledger.execute(
        """CREATE TRIGGER reject_price BEFORE INSERT ON ksf_normalized_features
           WHEN NEW.feature_name = 'volume' BEGIN SELECT RAISE(ABORT, 'reject volume'); END"""
    )
    records = DomesticPriceCollector(
        client=FakePriceClient({"005930": [{"stck_bsop_date": "20260731", "stck_clpr": "80000", "acml_vol": "1"}]})
    ).collect_symbols(["005930"], available_data_cutoff=CUTOFF)

    with pytest.raises(sqlite3.IntegrityError, match="reject volume"):
        DomesticPriceRepository(ledger).store_records(records, as_of_kst=CUTOFF, available_data_cutoff=CUTOFF)

    assert ledger.execute("SELECT COUNT(*) FROM ksf_runs").fetchone()[0] == 0
    assert ledger.execute("SELECT COUNT(*) FROM ksf_source_snapshots").fetchone()[0] == 0
    assert ledger.execute("SELECT COUNT(*) FROM ksf_normalized_features").fetchone()[0] == 0
