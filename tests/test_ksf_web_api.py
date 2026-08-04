from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.app import _require_ksf_auth, app


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "001_k_semiconductor_flow_desk_permanent_ledgers.sql"
AUTH_VALUE = "local-read-key"
SYMBOLS = ("005930", "000660")
FORBIDDEN = (
    "rank", "relative_rank", "pair_signal", "order", "buy", "sell",
    "매수", "매도", "상대", "대비", "비교", "순위",
)


def _make_ledger(path: Path, *, populated: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATION.read_text(encoding="utf-8"))
    if not populated:
        conn.commit()
        conn.close()
        return

    for index, symbol in enumerate(SYMBOLS):
        run_id = f"run-{symbol}"
        decision_id = f"decision-{symbol}"
        request_id = f"request-{symbol}"
        conn.execute(
            """INSERT INTO ksf_runs
               (run_id, symbol, trading_date, run_status, as_of_kst, available_data_cutoff)
               VALUES (?, ?, '2026-08-01', ?, '2026-08-01T16:00:00+09:00',
                       '2026-08-01T15:59:00+09:00')""",
            (run_id, symbol, "STALE_DATA" if index == 0 else "PARTIAL_DATA"),
        )
        features = {
            "foreign_net_flow": 1200.0 + index,
            "institution_net_flow": 300.0 + index,
            "individual_net_flow": -1500.0 - index,
            "nvda_adjusted_close": 182.0,
            "soxx_adjusted_close": 311.0,
            "tsm_adjusted_close": 204.0,
            "usdkrw": 1382.5,
            "mu_adjusted_close": 148.0,
            "dram_price": 2.1,
            "nand_price": 4.2,
            "hbm_supply_tightness": 0.8,
        }
        for feature_index, (name, value) in enumerate(features.items()):
            status = "STALE" if name == "dram_price" else "READY"
            conn.execute(
                """INSERT INTO ksf_normalized_features
                   (feature_id, run_id, symbol, feature_group, feature_name,
                    feature_version, feature_status, value_num, source_as_of,
                    ingested_at_kst, available_data_cutoff)
                   VALUES (?, ?, ?, 'fixture', ?, 'v1', ?, ?,
                           '2026-08-01T06:00:00+00:00',
                           '2026-08-01T15:30:00+09:00',
                           '2026-08-01T15:59:00+09:00')""",
                (f"feature-{symbol}-{feature_index}", run_id, symbol, name, status, value),
            )
        conn.execute(
            """INSERT INTO ksf_normalized_features
               (feature_id, run_id, symbol, feature_group, feature_name,
                feature_version, feature_status, source_as_of, ingested_at_kst,
                available_data_cutoff, missing_reason)
               VALUES (?, ?, ?, 'fixture', 'memory_license', 'v1',
                       'LICENSE_BLOCKED', '2026-08-01T06:00:00+00:00',
                       '2026-08-01T15:30:00+09:00',
                       '2026-08-01T15:59:00+09:00', 'license review')""",
            (f"feature-{symbol}-license", run_id, symbol),
        )
        conn.execute(
            """INSERT INTO ksf_decisions
               (decision_id, run_id, symbol, horizon_days, as_of_kst,
                available_data_cutoff, deterministic_score, score_label,
                user_opinion, scoring_ruleset_version, feature_snapshot_sha256,
                feature_contributions_json)
               VALUES (?, ?, ?, 5, '2026-08-01T16:00:00+09:00',
                       '2026-08-01T15:59:00+09:00', 20, 'positive_watch',
                       'WATCH', 'v1', ?, ?)""",
            (decision_id, run_id, symbol, f"hash-{symbol}", json.dumps([
                {"feature_name": "foreign_net_flow", "contribution": 12},
                {"feature_name": "dram_price", "contribution": -4},
            ])),
        )
        conn.execute(
            """INSERT INTO ksf_ai_requests
               (ai_request_id, run_id, symbol, purpose, as_of_kst,
                available_data_cutoff, prompt_template_version,
                redaction_policy_version, input_ledger_hash_sha256,
                prompt_hash_sha256, model_provider, model_name)
               VALUES (?, ?, ?, 'explain_decision',
                       '2026-08-01T16:00:00+09:00',
                       '2026-08-01T15:59:00+09:00', 'v1', 'v1', ?, ?,
                       'offline', 'fixture')""",
            (request_id, run_id, symbol, f"input-{symbol}", f"prompt-{symbol}"),
        )
        conn.execute(
            """INSERT INTO ksf_ai_responses
               (ai_response_id, ai_request_id, run_id, symbol, response_status,
                response_hash_sha256, summary, drivers_json, risks_json,
                model_provider, model_name)
               VALUES (?, ?, ?, ?, 'OK', ?, '단일 종목 흐름 요약', ?, ?,
                       'offline', 'fixture')""",
            (f"response-{symbol}", request_id, run_id, symbol, f"response-hash-{symbol}",
             json.dumps(["외국인 수급 확인"], ensure_ascii=False),
             json.dumps(["메모리 지표 시차"], ensure_ascii=False)),
        )
        conn.execute(
            """INSERT INTO ksf_performance_settlements
               (settlement_id, decision_id, run_id, symbol, horizon_days,
                base_trade_date, target_trade_date, settlement_status,
                base_close, target_close, return_bps, direction_hit,
                due_after_kst, settled_at_kst)
               VALUES (?, ?, ?, ?, 5, '2026-07-24', '2026-07-31', 'SETTLED',
                       100, 103, 300, 1, '2026-07-31T16:00:00+09:00',
                       '2026-07-31T16:01:00+09:00')""",
            (f"settlement-{symbol}", decision_id, run_id, symbol),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "ledger.sqlite3"
    _make_ledger(db_path)
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {AUTH_VALUE}"}


def _insert_run_feature(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    symbol: str,
    as_of: str,
    cutoff: str,
    feature_id: str,
    feature_name: str,
    value: float,
    source_as_of: str,
    ingested_at: str,
    status: str = "READY",
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO ksf_runs
           (run_id, symbol, trading_date, run_status, as_of_kst, available_data_cutoff)
           VALUES (?, ?, '2026-08-01', 'READY', ?, ?)""",
        (run_id, symbol, as_of, cutoff),
    )
    conn.execute(
        """INSERT INTO ksf_normalized_features
           (feature_id, run_id, symbol, feature_group, feature_name,
            feature_version, feature_status, value_num, source_as_of,
            ingested_at_kst, available_data_cutoff)
           VALUES (?, ?, ?, 'regression', ?, 'v1', ?, ?, ?, ?, ?)""",
        (feature_id, run_id, symbol, feature_name, status, value,
         source_as_of, ingested_at, cutoff),
    )


def test_card_merges_feature_families_by_absolute_cutoff_and_symbol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "split-runs.sqlite3"
    _make_ledger(db_path)
    conn = sqlite3.connect(db_path)

    # The baseline run is newest, but only contains global/memory collector output.
    _insert_run_feature(
        conn, run_id="card-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="card-nvda", feature_name="nvda_adjusted_close", value=190.0,
        source_as_of="2026-08-01T06:59:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
    )
    _insert_run_feature(
        conn, run_id="card-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="card-dram", feature_name="dram_price", value=2.5,
        source_as_of="2026-08-01T06:58:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        status="STALE",
    )
    # Eligible older domestic run. The +00:00 source time is later in absolute
    # time than the repeated +09:00 row and must win deterministically.
    _insert_run_feature(
        conn, run_id="domestic-005930", symbol="005930",
        as_of="2026-08-01T17:00:00+09:00", cutoff="2026-08-01T15:50:00+09:00",
        feature_id="domestic-foreign-new", feature_name="foreign_net_flow", value=555.0,
        source_as_of="2026-08-01T06:40:00+00:00", ingested_at="2026-08-01T15:45:00+09:00",
    )
    _insert_run_feature(
        conn, run_id="domestic-repeat-005930", symbol="005930",
        as_of="2026-08-01T16:30:00+09:00", cutoff="2026-08-01T15:45:00+09:00",
        feature_id="domestic-foreign-old", feature_name="foreign_net_flow", value=444.0,
        source_as_of="2026-08-01T15:35:00+09:00", ingested_at="2026-08-01T15:40:00+09:00",
    )
    # Older run whose cutoff is later than the card cutoff must not leak in.
    _insert_run_feature(
        conn, run_id="future-cutoff-005930", symbol="005930",
        as_of="2026-08-01T17:30:00+09:00", cutoff="2026-08-01T16:30:00+09:00",
        feature_id="future-institution", feature_name="institution_net_flow", value=9999.0,
        source_as_of="2026-08-01T07:20:00+00:00", ingested_at="2026-08-01T16:25:00+09:00",
    )
    # Same feature in the other supported symbol must remain independent.
    _insert_run_feature(
        conn, run_id="card-000660", symbol="000660",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="other-foreign", feature_name="foreign_net_flow", value=660.0,
        source_as_of="2026-08-01T06:45:00+00:00", ingested_at="2026-08-01T15:50:00+09:00",
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    cards = {card["symbol"]: card for card in TestClient(app).get("/ksf/cards", headers=_auth()).json()["cards"]}

    samsung = cards["005930"]
    assert samsung["baseline"]["as_of_kst"] == "2026-08-01T18:00:00+09:00"
    assert samsung["domestic_flow"]["foreign"] == {
        "value": 555.0, "status": "READY", "source_as_of": "2026-08-01T06:40:00+00:00",
    }
    assert samsung["domestic_flow"]["institution"]["value"] == 300.0
    assert samsung["global_environment"]["nvda"]["value"] == 190.0
    assert samsung["memory_indicators"]["dram"]["status"] == "STALE"
    assert all(item["value"] is not None for item in samsung["domestic_flow"].values())
    assert all(item["value"] is not None for item in samsung["global_environment"].values())
    assert all(
        samsung["memory_indicators"][name]["value"] is not None
        for name in ("mu", "dram", "nand", "hbm")
    )
    assert samsung["memory_indicators"]["license_blocked"] is True
    assert samsung["data_quality"]["has_stale"] is True
    assert cards["000660"]["domestic_flow"]["foreign"]["value"] == 660.0


@pytest.mark.parametrize("path", ["/ksf/cards", "/ksf/cards/005930"])
def test_json_api_requires_valid_bearer(client: TestClient, path: str):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    with pytest.raises(Exception) as exc_info:
        _require_ksf_auth("Bearer 잘못된토큰")
    assert getattr(exc_info.value, "status_code", None) == 401
    assert client.get(path, headers=_auth()).status_code == 200


def test_cards_are_exactly_two_independent_safe_entries(client: TestClient):
    response = client.get("/ksf/cards", headers=_auth())
    assert response.status_code == 200
    cards = response.json()["cards"]
    assert [card["symbol"] for card in cards] == list(SYMBOLS)
    for card in cards:
        assert {
            "domestic_flow", "global_environment", "memory_indicators",
            "evidence", "counterarguments", "baseline", "data_quality",
            "prior_performance",
        } <= card.keys()
        encoded = json.dumps(card, ensure_ascii=False).lower()
        assert not any(word in encoded for word in FORBIDDEN)


def test_detail_has_lineage_quality_performance_and_gap_flags(client: TestClient):
    detail = client.get("/ksf/cards/005930", headers=_auth()).json()
    assert detail["symbol"] == "005930"
    assert detail["evidence"] == ["외국인 수급 확인"]
    assert detail["counterarguments"] == ["메모리 지표 시차"]
    assert detail["baseline"]["as_of_kst"]
    assert detail["baseline"]["available_data_cutoff"]
    assert detail["baseline"]["source_as_of"]
    assert detail["data_quality"]["state"] == "stale"
    assert detail["data_quality"]["has_stale"] is True
    assert detail["data_quality"]["has_missing"] is True
    assert detail["prior_performance"][0] == {
        "horizon_days": 5,
        "status": "SETTLED",
        "direction_hit": True,
        "return_bps": 300.0,
    }


def test_unsupported_detail_is_404(client: TestClient):
    assert client.get("/ksf/cards/123456", headers=_auth()).status_code == 404


def test_empty_ledger_returns_no_data_cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "empty.sqlite3"
    _make_ledger(db_path, populated=False)
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    client = TestClient(app)
    response = client.get("/ksf/cards", headers=_auth())
    assert response.status_code == 200
    assert [card["symbol"] for card in response.json()["cards"]] == list(SYMBOLS)
    assert all(card["data_quality"]["state"] == "no_data" for card in response.json()["cards"])
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["data_quality"]["state"] == "no_data"
    page = client.get("/ksf/005930", headers=_auth())
    assert page.status_code == 200
    assert "no_data" in page.text


@pytest.mark.parametrize("path", ["/ksf", "/ksf/005930"])
def test_html_is_authenticated_mobile_first_and_read_only(client: TestClient, path: str):
    assert client.get(path).status_code == 401
    response = client.get(path, headers=_auth())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text.lower()
    assert 'name="viewport"' in html
    assert "@media" in html
    assert "<form" not in html
    assert "<button" not in html
    assert 'method="post"' not in html
    assert not any(word in html for word in FORBIDDEN)
