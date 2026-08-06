from __future__ import annotations

import base64
import json
import os
import re
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.app import _basic_valid, _require_ksf_auth, app


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "001_k_semiconductor_flow_desk_permanent_ledgers.sql"
SERVICE_UNIT = Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "kronostock-dashboard.service"
AUTH_VALUE = "local-read-key"
BASIC_USER = "ksf-viewer"
BASIC_PASSWORD = "browser-read-secret"
SYMBOLS = ("005930", "000660")
FORBIDDEN = (
    "rank", "relative_rank", "pair_signal", "order", "buy", "sell",
    "매수", "매도", "상대", "대비", "비교", "순위",
    # 라이선스 taxonomy/정책 메타데이터는 어떤 공개 응답에도 실리면 안 된다.
    "license_blocked", "do_not_display_or_score", "display_policy",
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
            # 005930(STALE_DATA run)만 STALE 피처를 갖는다. 000660 은 프로덕션과 동일한
            # 비 stale PARTIAL_DATA 카드로 유지해 partial headline 계약을 검증한다.
            status = "STALE" if name == "dram_price" and index == 0 else "READY"
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
    monkeypatch.delenv("KSF_READ_USERNAME", raising=False)
    monkeypatch.delenv("KSF_READ_PASSWORD", raising=False)
    return TestClient(app)


@pytest.fixture
def basic_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("KSF_READ_USERNAME", BASIC_USER)
    monkeypatch.setenv("KSF_READ_PASSWORD", BASIC_PASSWORD)
    return client


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {AUTH_VALUE}"}


def _basic(user: str = BASIC_USER, password: str = BASIC_PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _insert_run_feature(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    symbol: str,
    as_of: str,
    cutoff: str,
    feature_id: str,
    feature_name: str,
    value: float | None,
    source_as_of: str,
    ingested_at: str,
    status: str = "READY",
    value_text: str | None = None,
    value_json: str | None = None,
    missing_reason: str | None = None,
    unit: str | None = None,
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
            feature_version, feature_status, value_num, value_text, value_json,
            unit, source_as_of, ingested_at_kst, available_data_cutoff, missing_reason)
           VALUES (?, ?, ?, 'regression', ?, 'v1', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (feature_id, run_id, symbol, feature_name, status, value, value_text,
         value_json, unit, source_as_of, ingested_at, cutoff, missing_reason),
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
    # 차단 항목은 공개 boolean 으로 노출하지 않는다 — 결측 플래그로만 정직하게 반영.
    assert "license_blocked" not in samsung["memory_indicators"]
    assert samsung["data_quality"]["has_missing"] is True
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
    # HTML 은 raw taxonomy 대신 한국어 공개 라벨을 렌더링한다 (JSON 은 no_data 유지).
    assert "데이터 없음" in page.text


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


@pytest.mark.parametrize("path", ["/ksf", "/ksf/005930", "/ksf/cards", "/ksf/cards/005930"])
def test_valid_basic_auth_grants_read_access(basic_client: TestClient, path: str):
    response = basic_client.get(path, headers=_basic())
    assert response.status_code == 200


def test_basic_auth_html_navigation_and_read_only(basic_client: TestClient):
    listing = basic_client.get("/ksf", headers=_basic())
    assert listing.status_code == 200
    assert listing.headers["content-type"].startswith("text/html")
    assert 'href="/ksf/005930"' in listing.text
    assert 'href="/ksf/000660"' in listing.text
    detail = basic_client.get("/ksf/005930", headers=_basic())
    assert detail.status_code == 200
    assert 'href="/ksf"' in detail.text
    for text in (listing.text.lower(), detail.text.lower()):
        assert "<form" not in text
        assert "<button" not in text


_NO_COLON_B64 = base64.b64encode(b"no-colon-here").decode("ascii")
_BROKEN_UTF8_B64 = base64.b64encode(b"\xff\xfe:broken-utf8").decode("ascii")
_VALID_PAIR_B64 = base64.b64encode(f"{BASIC_USER}:{BASIC_PASSWORD}".encode("utf-8")).decode("ascii")


@pytest.mark.parametrize("headers", [
    _basic(password="wrong"),
    _basic(user="wrong"),
    _basic(user="", password=""),
    _basic(user="사용자", password="비밀번호"),
    {"Authorization": "Basic"},
    {"Authorization": "Basic "},
    {"Authorization": "Basic %%%not-base64%%%"},
    {"Authorization": f"Basic {_NO_COLON_B64}"},
    {"Authorization": f"Basic {_BROKEN_UTF8_B64}"},
    {"Authorization": f"basic {_VALID_PAIR_B64} extra"},
])
def test_invalid_or_malformed_basic_is_401_without_leaking(basic_client: TestClient, headers: dict[str, str]):
    response = basic_client.get("/ksf/cards", headers=headers)
    assert response.status_code == 401
    assert BASIC_PASSWORD not in response.text
    assert AUTH_VALUE not in response.text


@pytest.mark.parametrize("configured", [
    {},
    {"KSF_READ_USERNAME": BASIC_USER},
    {"KSF_READ_PASSWORD": BASIC_PASSWORD},
    {"KSF_READ_USERNAME": BASIC_USER, "KSF_READ_PASSWORD": ""},
    {"KSF_READ_USERNAME": "", "KSF_READ_PASSWORD": BASIC_PASSWORD},
])
def test_basic_auth_fails_closed_without_full_config(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, configured: dict[str, str]
):
    for name, value in configured.items():
        monkeypatch.setenv(name, value)
    assert client.get("/ksf/cards", headers=_basic()).status_code == 401
    assert client.get("/ksf", headers=_basic()).status_code == 401
    # Basic 구성 상태와 무관하게 기존 Bearer 는 계속 동작해야 한다.
    assert client.get("/ksf/cards", headers=_auth()).status_code == 200


def test_bearer_regression_with_basic_configured(basic_client: TestClient):
    assert basic_client.get("/ksf/cards", headers=_auth()).status_code == 200
    assert basic_client.get("/ksf", headers=_auth()).status_code == 200
    assert basic_client.get("/ksf/cards", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_401_advertises_basic_and_bearer_challenges(basic_client: TestClient):
    response = basic_client.get("/ksf")
    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert "Basic" in challenge
    assert "realm=" in challenge
    assert "Bearer" in challenge
    for secret in (AUTH_VALUE, BASIC_PASSWORD):
        assert secret not in challenge
        assert secret not in response.text


def test_query_string_credentials_are_rejected(basic_client: TestClient):
    queries = (
        f"token={AUTH_VALUE}",
        f"access_token={AUTH_VALUE}",
        f"username={BASIC_USER}&password={BASIC_PASSWORD}",
    )
    for query in queries:
        assert basic_client.get(f"/ksf/cards?{query}").status_code == 401
        assert basic_client.get(f"/ksf?{query}").status_code == 401


# --- systemd unit: 주석/중복 지시어로는 검증을 통과할 수 없도록 실제 파싱한다 ---


def _unit_directives(text: str) -> dict[tuple[str, str], list[str]]:
    """(section, key) -> 등장 순서대로의 값 리스트. 주석(#, ;) 줄은 무시."""
    directives: dict[tuple[str, str], list[str]] = {}
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        assert sep, f"malformed unit line: {raw_line!r}"
        directives.setdefault((section, key.strip()), []).append(value.strip())
    return directives


def _effective_scalar(directives: dict[tuple[str, str], list[str]], section: str, key: str) -> str | None:
    values = directives.get((section, key))
    return values[-1] if values else None


def _effective_paths(directives: dict[tuple[str, str], list[str]], section: str, key: str) -> list[str]:
    """systemd 리스트 지시어 의미론: 빈 대입은 리스트를 초기화한다."""
    result: list[str] = []
    for value in directives.get((section, key), []):
        if value == "":
            result = []
        else:
            result.extend(value.split())
    return result


def _service_unit_directives() -> dict[tuple[str, str], list[str]]:
    return _unit_directives(SERVICE_UNIT.read_text(encoding="utf-8"))


def test_dashboard_service_unit_is_loopback_only_and_hardened():
    directives = _service_unit_directives()
    assert _effective_scalar(directives, "Service", "User") == "deploy"
    assert _effective_scalar(directives, "Service", "Group") == "deploy"
    assert _effective_scalar(directives, "Service", "WorkingDirectory") == "/srv/agent-workspaces/KronosStock"
    # 대시보드는 프로덕션 원장이 아니라 서비스 시작 시점 런타임 스냅샷만 읽는다.
    environment = _effective_paths(directives, "Service", "Environment")
    assert "KSF_LEDGER_DB_PATH=/run/kronostock-dashboard/ksf-ledger.sqlite3" in environment
    assert not any(
        entry.startswith("KSF_LEDGER_DB_PATH=") and "/srv/kronostock" in entry
        for entry in environment
    )
    exec_start = _effective_scalar(directives, "Service", "ExecStart")
    assert exec_start is not None
    args = exec_start.split()
    assert "--host" in args and args[args.index("--host") + 1] == "127.0.0.1"
    assert "--port" in args and args[args.index("--port") + 1] == "8000"
    assert "0.0.0.0" not in args
    assert "--reload" not in args
    assert _effective_scalar(directives, "Service", "NoNewPrivileges") == "true"
    text = SERVICE_UNIT.read_text(encoding="utf-8")
    assert AUTH_VALUE not in text
    assert BASIC_PASSWORD not in text


def test_dashboard_service_unit_has_no_writable_filesystem_escape():
    """ProtectSystem=strict 아래에서 런타임 스냅샷 디렉터리 외 쓰기 경로를 열지 않는다."""
    directives = _service_unit_directives()
    assert _effective_scalar(directives, "Service", "ProtectSystem") == "strict"
    # 유일하게 허용되는 쓰기 경로는 RuntimeDirectory 인 /run/kronostock-dashboard 뿐이다.
    assert set(_effective_paths(directives, "Service", "ReadWritePaths")) <= {"/run/kronostock-dashboard"}
    for key in ("BindPaths", "BindReadOnlyPaths", "TemporaryFileSystem"):
        assert _effective_paths(directives, "Service", key) == [], f"{key} must not grant writable escapes"
    # ledger 프로덕션 데이터 디렉터리는 명시적으로 읽기 전용이어야 한다.
    assert "/srv/kronostock/data" in _effective_paths(directives, "Service", "ReadOnlyPaths")
    assert _effective_scalar(directives, "Service", "ProtectHome") == "read-only"
    assert _effective_scalar(directives, "Service", "UMask") == "0077"


def test_dashboard_service_unit_snapshots_ledger_into_private_runtime_dir():
    """Hermes blocker B: WAL 원장은 시작 시점 스냅샷으로만 읽는다 (프로덕션 쓰기 금지)."""
    directives = _service_unit_directives()
    assert _effective_scalar(directives, "Service", "RuntimeDirectory") == "kronostock-dashboard"
    assert _effective_scalar(directives, "Service", "RuntimeDirectoryMode") == "0700"
    exec_start_pre = directives.get(("Service", "ExecStartPre"), [])
    assert len(exec_start_pre) == 1
    pre_args = exec_start_pre[0].split()
    assert pre_args == [
        "/srv/agent-workspaces/KronosStock/.venv/bin/python",
        "/srv/agent-workspaces/KronosStock/scripts/deploy/prepare_ksf_dashboard_snapshot.py",
        "/srv/kronostock/data/ksf_ledger.sqlite3",
        "/run/kronostock-dashboard/ksf-ledger.sqlite3",
    ]
    # ExecStart 프로세스는 런타임 스냅샷만 바라본다.
    environment = _effective_paths(directives, "Service", "Environment")
    assert "KSF_LEDGER_DB_PATH=/run/kronostock-dashboard/ksf-ledger.sqlite3" in environment


def test_dashboard_service_unit_disables_access_log_and_uses_scoped_env_file():
    directives = _service_unit_directives()
    exec_start = _effective_scalar(directives, "Service", "ExecStart")
    assert exec_start is not None
    assert "--no-access-log" in exec_start.split()
    # 비밀값은 레포 전체 .env 가 아니라 대시보드 전용 최소권한 env 파일에서만 읽는다.
    env_files = directives.get(("Service", "EnvironmentFile"), [])
    assert env_files == ["/srv/kronostock/config/ksf-dashboard.env"]


def test_dashboard_service_unit_documents_ledger_lock_prerequisite_without_lock_write_grant():
    """Codex closure B: 스냅샷 헬퍼는 원장 사이드카 잠금(<ledger>.lock)을 O_CREAT 없이
    읽기 전용으로 열어 writer 를 배제한다. 유닛은 잠금 파일 배포 선행조건을 문서화하되
    /run/lock 등 추가 쓰기 경로는 절대 열지 않는다."""
    text = SERVICE_UNIT.read_text(encoding="utf-8")
    # 배포 선행조건 (0600, deploy 소유, 서비스 시작 전 생성) 이 유닛에 명시돼야 한다.
    assert "/srv/kronostock/data/ksf_ledger.sqlite3.lock" in text
    assert "0600" in text
    directives = _service_unit_directives()
    # 잠금은 읽기 전용 fd 위 flock 으로 잡으므로 쓰기 경로 추가 grant 가 없어야 한다.
    assert set(_effective_paths(directives, "Service", "ReadWritePaths")) == {"/run/kronostock-dashboard"}
    assert "/run/lock" not in text
    # ExecStartPre 는 여전히 source/destination 두 인자만 받는다 — 잠금 경로는
    # 헬퍼가 source 에서 자동 유도한다(다른 잠금 경로를 몰래 쓸 수 없음).
    exec_start_pre = directives.get(("Service", "ExecStartPre"), [])
    assert len(exec_start_pre) == 1
    assert exec_start_pre[0].split()[-2:] == [
        "/srv/kronostock/data/ksf_ledger.sqlite3",
        "/run/kronostock-dashboard/ksf-ledger.sqlite3",
    ]


# --- Codex blocker 1: LICENSE_BLOCKED / display_policy 값 억제 ---

_LICENSED_VALUE_JSON = json.dumps({
    "value": 0.91,
    "display_policy": "do_not_display_or_score",
    "source_url": "https://vendor.example.com/licensed-feed",
    "terms_url": "https://vendor.example.com/terms",
    "notes": "restricted-metadata",
})
# Hermes 재현 payload — value_num/value_text 가 이미 있는 행에 실려 온 정책 메타데이터.
_POLICY_WITH_URL_JSON = json.dumps({
    "display_policy": "do_not_display_or_score",
    "source_url": "https://blocked.example",
})


@pytest.fixture
def policy_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "policy.sqlite3"
    _make_ledger(db_path)
    conn = sqlite3.connect(db_path)
    # LICENSE_BLOCKED 인데 value_json 에 라이선스 메타데이터가 실려 온 경우.
    _insert_run_feature(
        conn, run_id="policy-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="policy-hbm", feature_name="hbm_supply_tightness", value=None,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        status="LICENSE_BLOCKED", value_json=_LICENSED_VALUE_JSON,
        missing_reason="license restriction",
    )
    # 상태는 READY 지만 value_json display_policy 가 표시 금지를 명시한 경우.
    _insert_run_feature(
        conn, run_id="policy-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="policy-nand", feature_name="nand_wafer_direction", value=None,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        status="READY", value_json=_LICENSED_VALUE_JSON,
    )
    # Hermes 재현: READY + value_num 이 이미 존재해도 display_policy 는 우회되면 안 된다.
    _insert_run_feature(
        conn, run_id="policy-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="policy-dram", feature_name="dram_spot_direction", value=4271.5,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        status="READY", value_json=_POLICY_WITH_URL_JSON,
    )
    # 동일 우회를 value_text 경로에서도 재현.
    _insert_run_feature(
        conn, run_id="policy-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="policy-mu", feature_name="mu_adjusted_close", value=None,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        status="READY", value_text="restricted-directional-call",
        value_json=_POLICY_WITH_URL_JSON,
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    monkeypatch.delenv("KSF_READ_USERNAME", raising=False)
    monkeypatch.delenv("KSF_READ_PASSWORD", raising=False)
    return TestClient(app)


# LICENSE_BLOCKED taxonomy 자체가 금지어다 — 원시 상태값(대소문자 무관)과 공개
# boolean 키("license_blocked") 모두 커버한다. 값/정책 메타데이터 금지어와 합쳐
# 소문자 비교로 검사한다.
_POLICY_FORBIDDEN = (
    "license_blocked", "display_policy", "do_not_display_or_score",
    "source_url", "terms_url",
    "vendor.example.com", "licensed-feed", "restricted-metadata", "0.91",
    "blocked.example", "4271.5", "restricted-directional-call",
)


def test_license_blocked_feature_value_is_suppressed_in_api(policy_client: TestClient):
    detail = policy_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    card = detail.json()
    hbm = card["memory_indicators"]["hbm"]
    # 차단 입력은 일반화된 UNAVAILABLE 로만 노출된다 — 원시 taxonomy 는 안전하지 않다.
    assert hbm == {
        "value": None,
        "status": "UNAVAILABLE",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    nand = card["memory_indicators"]["nand"]
    assert nand["value"] is None
    # 공개 license_blocked boolean 은 계약 위반이므로 존재하지 않아야 한다.
    assert "license_blocked" not in card["memory_indicators"]
    for needle in _POLICY_FORBIDDEN:
        assert needle not in detail.text.lower(), needle
    listing = policy_client.get("/ksf/cards", headers=_auth())
    assert listing.status_code == 200
    for needle in _POLICY_FORBIDDEN:
        assert needle not in listing.text.lower(), needle


def test_license_blocked_feature_value_is_suppressed_in_html(policy_client: TestClient):
    page = policy_client.get("/ksf/005930", headers=_auth())
    assert page.status_code == 200
    for needle in _POLICY_FORBIDDEN:
        assert needle not in page.text.lower(), needle
    # dict 가 str() 로 렌더링돼 새는 경우가 없어야 한다.
    assert "{'" not in page.text and "&#x27;value&#x27;" not in page.text
    # 차단 항목은 HTML 에서 한국어 공개 라벨(사용 제한)로만 표기한다.
    assert "사용 제한" in page.text


# --- 릴리스 블로커(356731c 프로덕션 스모크): taxonomy 는 4개 엔드포인트 어디에도 없다 ---

_KSF_ENDPOINTS = ("/ksf/cards", "/ksf/cards/005930", "/ksf", "/ksf/005930")


@pytest.mark.parametrize("path", _KSF_ENDPOINTS)
def test_base_fixture_license_taxonomy_never_serialized(client: TestClient, path: str):
    """기본 fixture 는 LICENSE_BLOCKED 행(memory_license)을 이미 포함한다."""
    response = client.get(path, headers=_auth())
    assert response.status_code == 200
    text = response.text.lower()
    for needle in ("license_blocked", "do_not_display_or_score", "display_policy",
                   "source_url", "terms_url"):
        assert needle not in text, needle


@pytest.mark.parametrize("path", _KSF_ENDPOINTS)
def test_policy_ledger_license_taxonomy_never_serialized(policy_client: TestClient, path: str):
    response = policy_client.get(path, headers=_auth())
    assert response.status_code == 200
    text = response.text.lower()
    for needle in _POLICY_FORBIDDEN:
        assert needle not in text, needle


def test_memory_indicators_expose_only_indicator_keys(client: TestClient):
    for card in client.get("/ksf/cards", headers=_auth()).json()["cards"]:
        assert set(card["memory_indicators"]) == {"mu", "dram", "nand", "hbm"}


def test_empty_card_contract_has_no_license_blocked_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "empty-taxonomy.sqlite3"
    _make_ledger(db_path, populated=False)
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    response = TestClient(app).get("/ksf/cards", headers=_auth())
    assert response.status_code == 200
    assert "license_blocked" not in response.text.lower()
    for card in response.json()["cards"]:
        assert card["memory_indicators"] == {}
        # 결측 자체는 계속 정직하게 표시한다.
        assert card["data_quality"]["has_missing"] is True


def test_blocked_only_input_headlines_restricted_and_preserves_missing_truth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """차단 전용 입력은 공개 상태 restricted 로 표기하되 has_missing 진실은 유지하고
    taxonomy 는 계속 일반화한다 (승인된 B 리서치 데스크 상태 계약)."""
    db_path = tmp_path / "truthful.sqlite3"
    _make_ledger(db_path, populated=False)
    conn = sqlite3.connect(db_path)
    _insert_run_feature(
        conn, run_id="truthful-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="truthful-nvda", feature_name="nvda_adjusted_close", value=190.0,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
    )
    _insert_run_feature(
        conn, run_id="truthful-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="truthful-hbm", feature_name="hbm_supply_tightness", value=None,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        status="LICENSE_BLOCKED", missing_reason="license review",
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    detail = TestClient(app).get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    card = detail.json()
    assert card["data_quality"]["has_missing"] is True
    assert card["data_quality"]["has_stale"] is False
    # 결측/부분 신호 없이 차단 항목만 있으면 공개 상태는 missing 이 아니라 restricted.
    assert card["data_quality"]["state"] == "restricted"
    assert card["data_quality"].get("has_restricted") is True
    assert card["memory_indicators"]["hbm"] == {
        "value": None,
        "status": "UNAVAILABLE",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    assert "license_blocked" not in detail.text.lower()


def test_rogue_license_blocked_source_status_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """source_statuses 대체 경로도 taxonomy 를 새지 않는다.

    스키마 CHECK 는 이 값을 허용하지 않지만, 미래 마이그레이션/수동 조작으로
    들어온 rogue 행까지 방어해야 하므로 CHECK 를 끄고 주입해 검증한다.
    """
    db_path = tmp_path / "rogue-source.sqlite3"
    _make_ledger(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        """INSERT INTO ksf_collected_source_metadata
           (metadata_id, source_name, capture_date, quality_grade, raw_storage_policy)
           VALUES ('rogue-meta', 'memory_license_review', '2026-08-01', 'D', 'NONE')"""
    )
    conn.execute(
        """INSERT INTO ksf_source_snapshots
           (snapshot_id, run_id, symbol, source_metadata_id, source_name,
            source_kind, source_status, source_as_of, ingested_at_kst,
            available_data_cutoff, quality_grade, missing_reason)
           VALUES ('rogue-snapshot', NULL, '005930', 'rogue-meta',
                   'memory_license_review', 'memory_license_review',
                   'LICENSE_BLOCKED', '2026-08-01T06:30:00+00:00',
                   '2026-08-01T15:30:00+09:00', '2026-08-01T15:59:00+09:00',
                   'D', 'license review')"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    client = TestClient(app)
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    statuses = {
        row["source"]: row["status"]
        for row in detail.json()["data_quality"]["source_statuses"]
    }
    assert statuses["memory_license_review"] == "UNAVAILABLE"
    assert "license_blocked" not in detail.text.lower()
    page = client.get("/ksf/005930", headers=_auth())
    assert "license_blocked" not in page.text.lower()


# --- Hermes blocker A: display_policy 는 value_num/value_text 존재와 무관하게 평가 ---

def test_display_policy_suppresses_existing_value_num(policy_client: TestClient):
    detail = policy_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    dram = detail.json()["memory_indicators"]["dram"]
    assert dram == {
        "value": None,
        "status": "READY",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    for response in (detail, policy_client.get("/ksf/cards", headers=_auth()),
                     policy_client.get("/ksf/005930", headers=_auth())):
        assert "4271.5" not in response.text
        assert "blocked.example" not in response.text


def test_display_policy_suppresses_existing_value_text(policy_client: TestClient):
    detail = policy_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    mu = detail.json()["memory_indicators"]["mu"]
    assert mu == {
        "value": None,
        "status": "READY",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    for response in (detail, policy_client.get("/ksf/cards", headers=_auth()),
                     policy_client.get("/ksf/005930", headers=_auth())):
        assert "restricted-directional-call" not in response.text


def test_license_blocked_short_circuits_without_parsing_value_json(monkeypatch: pytest.MonkeyPatch):
    from ksf import web_api

    row = {
        "feature_status": "LICENSE_BLOCKED",
        "value_num": 1.0,
        "value_text": "leak",
        "value_json": _LICENSED_VALUE_JSON,
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }

    def _explode(*args, **kwargs):
        raise AssertionError("LICENSE_BLOCKED must not parse value_json")

    monkeypatch.setattr(web_api.json, "loads", _explode)
    # 단락은 유지하되, 밖으로 나가는 상태는 원시 taxonomy 가 아닌 일반화된 값이다.
    assert web_api._feature_value({"hbm_supply_tightness": row}, ("hbm_supply_tightness",)) == {
        "value": None,
        "status": "UNAVAILABLE",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }


def test_malformed_unrelated_value_json_keeps_scalar_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "malformed.sqlite3"
    _make_ledger(db_path)
    conn = sqlite3.connect(db_path)
    _insert_run_feature(
        conn, run_id="malformed-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="malformed-nvda", feature_name="nvda_adjusted_close", value=190.5,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        value_json='{"broken',
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    detail = TestClient(app).get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["global_environment"]["nvda"] == {
        "value": 190.5,
        "status": "READY",
        "unit": "USD",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }


def test_dict_value_json_without_policy_never_exposes_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "metadata.sqlite3"
    _make_ledger(db_path)
    conn = sqlite3.connect(db_path)
    _insert_run_feature(
        conn, run_id="metadata-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="metadata-soxx", feature_name="soxx_adjusted_close", value=None,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        value_json=json.dumps({"value": 305.5, "source_url": "https://vendor2.example/feed"}),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    client = TestClient(app)
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    soxx = detail.json()["global_environment"]["soxx"]
    assert soxx["value"] == 305.5  # dict payload 에서 안전한 스칼라 값만 추출
    assert "vendor2.example" not in detail.text
    assert "source_url" not in detail.text
    page = client.get("/ksf/005930", headers=_auth())
    assert "vendor2.example" not in page.text


# --- Codex blocker 3: 인증 스킴 대소문자 무시 (자격증명 파싱은 엄격 유지) ---

@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr"])
def test_bearer_scheme_is_case_insensitive(client: TestClient, scheme: str):
    assert client.get("/ksf/cards", headers={"Authorization": f"{scheme} {AUTH_VALUE}"}).status_code == 200
    assert client.get("/ksf/cards", headers={"Authorization": f"{scheme} wrong"}).status_code == 401


@pytest.mark.parametrize("scheme", ["basic", "BASIC", "BaSiC"])
def test_basic_scheme_is_case_insensitive(basic_client: TestClient, scheme: str):
    token = base64.b64encode(f"{BASIC_USER}:{BASIC_PASSWORD}".encode("utf-8")).decode("ascii")
    assert basic_client.get("/ksf/cards", headers={"Authorization": f"{scheme} {token}"}).status_code == 200
    assert basic_client.get("/ksf", headers={"Authorization": f"{scheme} {token}"}).status_code == 200
    wrong = base64.b64encode(b"ksf-viewer:wrong").decode("ascii")
    assert basic_client.get("/ksf/cards", headers={"Authorization": f"{scheme} {wrong}"}).status_code == 401
    # 스킴만 정규화한다: 자격증명 뒤에 토큰이 더 붙으면 여전히 거부.
    assert basic_client.get("/ksf/cards", headers={"Authorization": f"{scheme} {token} extra"}).status_code == 401


# --- Codex 항목 5: 기대 자격증명 env 에 서러게이트가 있으면 500 이 아니라 401 ---

def test_surrogate_expected_basic_credentials_fail_closed(basic_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KSF_READ_USERNAME", BASIC_USER + "\udcff")
    monkeypatch.setenv("KSF_READ_PASSWORD", BASIC_PASSWORD + "\udcff")
    response = basic_client.get("/ksf/cards", headers=_basic())
    assert response.status_code == 401
    assert _basic_valid(base64.b64encode(f"{BASIC_USER}:{BASIC_PASSWORD}".encode()).decode("ascii")) is False


def test_surrogate_expected_bearer_token_fails_closed(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE + "\udcff")
    assert client.get("/ksf/cards", headers=_auth()).status_code == 401


# --- Codex 항목 6: WWW-Authenticate 문법을 구조적으로 검증 ---

def _parse_challenges(header: str) -> dict[str, dict[str, str]]:
    challenges: dict[str, dict[str, str]] = {}
    current: str | None = None
    for part in header.split(","):
        part = part.strip()
        head, _, rest = part.partition(" ")
        if "=" not in head:
            # 새 챌린지 시작: "Scheme" 또는 "Scheme key=value"
            current = head
            challenges[current] = {}
            part = rest.strip()
            if not part:
                continue
        assert current is not None, f"parameter before any scheme: {part!r}"
        key, sep, value = part.partition("=")
        assert sep, f"bare token inside challenge params: {part!r}"
        challenges[current][key.strip()] = value.strip().strip('"')
    return challenges


def test_401_challenge_grammar_is_unambiguous(basic_client: TestClient):
    response = basic_client.get("/ksf")
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge == 'Basic realm="KronosStock KSF", charset="UTF-8", Bearer realm="KronosStock KSF"'
    assert _parse_challenges(challenge) == {
        "Basic": {"realm": "KronosStock KSF", "charset": "UTF-8"},
        "Bearer": {"realm": "KronosStock KSF"},
    }


# --- Codex blocker 4: 유닛 ExecStart 그대로 띄웠을 때 자격증명이 로그에 남지 않는다 ---

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"service exited early with code {proc.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    raise AssertionError("service did not become healthy in time")


def _get_status(port: int, path_qs: str, headers: dict[str, str] | None = None) -> int:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path_qs}", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_service_execstart_does_not_log_credentials(tmp_path: Path):
    directives = _service_unit_directives()
    exec_start = _effective_scalar(directives, "Service", "ExecStart")
    assert exec_start is not None
    port = _free_port()
    argv = [str(port) if arg == "8000" else arg for arg in exec_start.split()]
    db_path = tmp_path / "ledger.sqlite3"
    _make_ledger(db_path)
    env = {
        **os.environ,
        "KSF_LEDGER_DB_PATH": str(db_path),
        "KSF_READ_TOKEN": AUTH_VALUE,
        "KSF_READ_USERNAME": BASIC_USER,
        "KSF_READ_PASSWORD": BASIC_PASSWORD,
    }
    proc = subprocess.Popen(
        argv,
        cwd=str(SERVICE_UNIT.parents[2]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_health(port, proc)
        assert _get_status(port, f"/ksf/cards?token={AUTH_VALUE}") == 401
        assert _get_status(port, f"/ksf?username={BASIC_USER}&password={BASIC_PASSWORD}") == 401
        assert _get_status(port, "/ksf/cards", {"Authorization": "Bearer wrong-secret-value"}) == 401
        assert _get_status(port, "/ksf/cards", _auth()) == 200
    finally:
        proc.terminate()
        try:
            logs, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            logs, _ = proc.communicate()
    for secret in (AUTH_VALUE, BASIC_PASSWORD, "wrong-secret-value", "token=", "password="):
        assert secret not in logs, f"credential material leaked into service logs: {secret!r}"
    # 액세스 로그 자체가 꺼져 있어야 한다 (요청 라인 없음).
    assert "GET /ksf" not in logs


# --- Codex BLOCKED 3건: malformed 정책 JSON fail-open / AI 설명 미필터 / source_name 미살균 ---

# 정책 마커를 실은 채 잘린(파싱 불가) value_json — 스칼라 fallback 으로 새면 안 된다.
_MALFORMED_POLICY_JSON = '{"display_policy": "do_not_display_or_score", "source_url": "https://blocked.example'
_MALFORMED_POLICY_JSON_MIXED_CASE = '{"Display_Policy": "DO_NOT_DISPLAY_OR_SCORE", "Terms_URL": "https://Blocked.Example'
# 금지 마커를 전혀 담지 않은 일반 벤더 라벨 — 절대 가려지면 안 된다.
_SAFE_VENDOR_LABEL = "trendforce_dramexchange_memory_license_review"
_ADV_SAFE_DRIVER = "외국인 수급 우호적"
_ADV_SAFE_RISK = "메모리 지표 변동성 확대"
_ADV_DRIVERS = [
    _ADV_SAFE_DRIVER,
    "내부 사유: LICENSE_BLOCKED 항목 제외",
    "자세한 근거는 https://vendor.example.com/report 참고",
    "Display_Policy=DO_NOT_DISPLAY_OR_SCORE 필터 적용",
    "출처 www.blocked-vendor.example 참조",
    "단기 매수 관점",  # 기존 트레이딩 금지어 필터가 계속 동작하는지 확인용
]
_ADV_RISKS = [
    _ADV_SAFE_RISK,
    "계약 조건은 terms_url 참조: https://vendor.example.com/terms",
    "SOURCE_URL 재검토 필요",
]
_ADV_FORBIDDEN_NEEDLES = (
    "license_blocked", "display_policy", "do_not_display_or_score",
    "source_url", "terms_url", "http://", "https://", "www.",
    "vendor.example.com", "blocked-vendor.example", "blocked.example",
    "licensed-feed", "6543.25", "8.75", "shielded-directional-text",
    "export_vendor",
)


@pytest.fixture
def adversarial_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "adversarial.sqlite3"
    _make_ledger(db_path)
    conn = sqlite3.connect(db_path)
    # rogue 대소문자 변형/스키마 외 상태값까지 방어해야 하므로 CHECK 를 끄고 주입한다.
    conn.execute("PRAGMA ignore_check_constraints = ON")
    # (1) 정책 마커를 실은 malformed value_json + 살아있는 value_num → 스칼라가 새면 안 됨.
    _insert_run_feature(
        conn, run_id="adv-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="adv-dram", feature_name="dram_spot_direction", value=6543.25,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        value_json=_MALFORMED_POLICY_JSON,
    )
    # 동일 우회를 value_text + 대소문자 변형 마커로 재현.
    _insert_run_feature(
        conn, run_id="adv-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="adv-mu", feature_name="mu_adjusted_close", value=None,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        value_text="shielded-directional-text", value_json=_MALFORMED_POLICY_JSON_MIXED_CASE,
    )
    # 정책과 무관한 malformed JSON 은 스칼라를 계속 보존해야 한다 (과차단 방지 대조군).
    _insert_run_feature(
        conn, run_id="adv-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="adv-soxx", feature_name="soxx_adjusted_close", value=307.25,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        value_json='{"broken',
    )
    # 대소문자 변형 LICENSE_BLOCKED 상태 — 값/상태 모두 일반화돼야 한다.
    _insert_run_feature(
        conn, run_id="adv-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="adv-nand", feature_name="nand_wafer_direction", value=8.75,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        status="License_Blocked", value_json=_LICENSED_VALUE_JSON,
    )
    # (2) AI 설명에 taxonomy/정책/URL 이 섞여 온 경우.
    conn.execute(
        """INSERT INTO ksf_ai_requests
           (ai_request_id, run_id, symbol, purpose, as_of_kst,
            available_data_cutoff, prompt_template_version,
            redaction_policy_version, input_ledger_hash_sha256,
            prompt_hash_sha256, model_provider, model_name)
           VALUES ('adv-request', 'adv-005930', '005930', 'explain_decision',
                   '2026-08-01T18:00:00+09:00', '2026-08-01T16:00:00+09:00',
                   'v1', 'v1', 'adv-input', 'adv-prompt', 'offline', 'fixture')"""
    )
    conn.execute(
        """INSERT INTO ksf_ai_responses
           (ai_response_id, ai_request_id, run_id, symbol, response_status,
            response_hash_sha256, summary, drivers_json, risks_json,
            model_provider, model_name)
           VALUES ('adv-response', 'adv-request', 'adv-005930', '005930', 'OK',
                   'adv-hash', '단일 종목 흐름 요약', ?, ?, 'offline', 'fixture')""",
        (json.dumps(_ADV_DRIVERS, ensure_ascii=False), json.dumps(_ADV_RISKS, ensure_ascii=False)),
    )
    # (3) 금지 텍스트를 담은 source_name + 대소문자 변형 상태.
    for metadata_id, source_name in (
        ("adv-meta-url", "see https://vendor.example.com/licensed-feed"),
        ("adv-meta-policy", "display_policy_export_vendor"),
        ("adv-meta-safe", _SAFE_VENDOR_LABEL),
    ):
        conn.execute(
            """INSERT INTO ksf_collected_source_metadata
               (metadata_id, source_name, capture_date, quality_grade, raw_storage_policy)
               VALUES (?, ?, '2026-08-01', 'D', 'NONE')""",
            (metadata_id, source_name),
        )
    for snapshot_id, metadata_id, source_name, source_status, grade in (
        ("adv-snapshot-url", "adv-meta-url", "see https://vendor.example.com/licensed-feed", "License_Blocked", "C"),
        ("adv-snapshot-policy", "adv-meta-policy", "display_policy_export_vendor", "COLLECTED", "B"),
        ("adv-snapshot-safe", "adv-meta-safe", _SAFE_VENDOR_LABEL, "COLLECTED", "A"),
    ):
        conn.execute(
            """INSERT INTO ksf_source_snapshots
               (snapshot_id, run_id, symbol, source_metadata_id, source_name,
                source_kind, source_status, source_as_of, ingested_at_kst,
                available_data_cutoff, quality_grade, missing_reason)
               VALUES (?, NULL, '005930', ?, ?, 'memory_license_review', ?,
                       '2026-08-01T06:30:00+00:00', '2026-08-01T15:30:00+09:00',
                       '2026-08-01T15:59:00+09:00', ?, NULL)""",
            (snapshot_id, metadata_id, source_name, source_status, grade),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    monkeypatch.delenv("KSF_READ_USERNAME", raising=False)
    monkeypatch.delenv("KSF_READ_PASSWORD", raising=False)
    return TestClient(app)


def test_malformed_policy_value_json_fails_closed_for_value_num(adversarial_client: TestClient):
    detail = adversarial_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["memory_indicators"]["dram"] == {
        "value": None,
        "status": "UNAVAILABLE",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    assert "6543.25" not in detail.text
    assert "blocked.example" not in detail.text.lower()


def test_malformed_policy_value_json_fails_closed_for_value_text(adversarial_client: TestClient):
    detail = adversarial_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["memory_indicators"]["mu"] == {
        "value": None,
        "status": "UNAVAILABLE",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    assert "shielded-directional-text" not in detail.text


def test_malformed_unrelated_value_json_still_preserves_scalar(adversarial_client: TestClient):
    """fail-closed 는 정책 마커가 보일 때만 — 무관한 깨진 JSON 은 기존 스칼라 유지."""
    detail = adversarial_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["global_environment"]["soxx"] == {
        "value": 307.25,
        "status": "READY",
        "unit": "USD",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }


def test_mixed_case_license_blocked_feature_status_fails_closed(adversarial_client: TestClient):
    detail = adversarial_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["memory_indicators"]["nand"] == {
        "value": None,
        "status": "UNAVAILABLE",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    assert "8.75" not in detail.text
    assert "license_blocked" not in detail.text.lower()


def test_adversarial_ai_explanations_are_dropped_entirely(adversarial_client: TestClient):
    detail = adversarial_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    card = detail.json()
    # 안전한 설명은 보존, 금지 텍스트를 담은 문자열은 문자열 전체를 폐기한다.
    assert card["evidence"] == [_ADV_SAFE_DRIVER]
    assert card["counterarguments"] == [_ADV_SAFE_RISK]
    # 기존 트레이딩 금지어 필터도 그대로 유지된다.
    assert "매수" not in json.dumps(card, ensure_ascii=False)


def test_adversarial_source_name_is_replaced_with_generic_label(adversarial_client: TestClient):
    detail = adversarial_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    sources = detail.json()["data_quality"]["source_statuses"]
    by_grade = {row["quality"]: row for row in sources}
    # 원문 source_name 은 절대 노출하지 않되 행 자체(품질/시각)는 보존한다.
    assert by_grade["C"] == {
        "source": "restricted_source",
        "status": "UNAVAILABLE",  # 대소문자 변형 License_Blocked 도 일반화
        "quality": "C",
        "source_as_of": "2026-08-01T06:30:00+00:00",
    }
    assert by_grade["B"]["source"] == "restricted_source"
    assert by_grade["B"]["status"] == "COLLECTED"
    # 금지 마커가 없는 일반 벤더 라벨은 그대로 유지된다.
    assert by_grade["A"]["source"] == _SAFE_VENDOR_LABEL
    assert by_grade["A"]["status"] == "COLLECTED"


@pytest.mark.parametrize("path", _KSF_ENDPOINTS)
def test_adversarial_ledger_leaks_nothing_on_any_endpoint(adversarial_client: TestClient, path: str):
    response = adversarial_client.get(path, headers=_auth())
    assert response.status_code == 200
    text = response.text.lower()
    for needle in _ADV_FORBIDDEN_NEEDLES:
        assert needle not in text, needle
    # 안전한 벤더 라벨을 과차단하지 않는다 (source_statuses 는 JSON 응답에만 실린다).
    if path.startswith("/ksf/cards"):
        assert _SAFE_VENDOR_LABEL in text


def test_mixed_case_blocked_status_headlines_restricted_and_preserves_missing_truth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """대소문자 변형 차단 상태도 has_missing/has_restricted 에 정직하게 반영되며
    공개 상태는 restricted 로 표기된다."""
    db_path = tmp_path / "mixed-case-truthful.sqlite3"
    _make_ledger(db_path, populated=False)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    _insert_run_feature(
        conn, run_id="mixed-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="mixed-nvda", feature_name="nvda_adjusted_close", value=190.0,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
    )
    _insert_run_feature(
        conn, run_id="mixed-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="mixed-hbm", feature_name="hbm_supply_tightness", value=None,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        status="License_Blocked", missing_reason="license review",
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    detail = TestClient(app).get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    card = detail.json()
    assert card["data_quality"]["has_missing"] is True
    assert card["data_quality"]["state"] == "restricted"
    assert card["data_quality"].get("has_restricted") is True
    assert card["memory_indicators"]["hbm"] == {
        "value": None,
        "status": "UNAVAILABLE",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    assert "license_blocked" not in detail.text.lower()


def test_public_text_policy_is_case_insensitive_and_conservative():
    from ksf import web_api

    assert web_api._public_status("LICENSE_BLOCKED") == "UNAVAILABLE"
    assert web_api._public_status("License_Blocked") == "UNAVAILABLE"
    assert web_api._public_status("lIcEnSe_bLoCkEd") == "UNAVAILABLE"
    assert web_api._public_status("BLOCKED_SEE_https://vendor.example") == "UNAVAILABLE"
    assert web_api._public_status("READY") == "READY"
    assert web_api._public_status(None) is None
    assert web_api._public_source_name("Terms_URL_export") == "restricted_source"
    assert web_api._public_source_name("WWW.evil.example") == "restricted_source"
    # 일반 벤더 라벨은 보수적 URL 전략으로 인해 가려지지 않는다.
    assert web_api._public_source_name(_SAFE_VENDOR_LABEL) == _SAFE_VENDOR_LABEL


# --- Codex BLOCKED (신규 HIGH): 유효 JSON + 대소문자 변형 정책 키가 억제를 우회 ---

# 파싱에 성공하는 유효 dict 인데 정책 키/값이 대소문자 변형이다 —
# payload.get("display_policy") 는 키를 exact-match 하므로 우회되고
# value_num/value_text/JSON 스칼라가 그대로 노출된다.
_MIXED_CASE_POLICY_JSON = json.dumps({
    "Display_Policy": "DO_NOT_DISPLAY_OR_SCORE",
    "Source_URL": "https://mixed-blocked.example/feed",
})
_MIXED_CASE_POLICY_SCALAR_JSON = json.dumps({
    "Display_Policy": "Do_Not_Display_Or_Score",
    "value": 6211.5,
})
# 서로 다른 케이스의 중복 display_policy 키 — 하나라도 차단을 명시하면 fail-closed.
_DUPLICATE_CASE_POLICY_JSON = (
    '{"display_policy": "unrelated_note", '
    '"DISPLAY_POLICY": "dO_nOt_DiSpLaY_oR_sCoRe", "value": 913.25}'
)
# 대조군: 대소문자 변형 정책 키라도 값이 차단이 아니면 스칼라 추출은 유지된다.
_MIXED_CASE_NON_BLOCKING_JSON = json.dumps({
    "Display_Policy": "OK_TO_DISPLAY",
    "value": 351.75,
})

_MIXED_CASE_FORBIDDEN = (
    "7433.25", "mixedcase-directional-text", "6211.5", "913.25",
    "mixed-blocked.example", "display_policy", "do_not_display_or_score",
    "source_url",
)


@pytest.fixture
def mixed_case_policy_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "mixed-case-policy.sqlite3"
    _make_ledger(db_path)
    conn = sqlite3.connect(db_path)
    # value_num 이 살아있는 행 + 대소문자 변형 정책 dict.
    _insert_run_feature(
        conn, run_id="mixedcase-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="mixedcase-dram", feature_name="dram_spot_direction", value=7433.25,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        value_json=_MIXED_CASE_POLICY_JSON,
    )
    # 동일 우회를 value_text 경로에서 재현.
    _insert_run_feature(
        conn, run_id="mixedcase-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="mixedcase-mu", feature_name="mu_adjusted_close", value=None,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        value_text="mixedcase-directional-text", value_json=_MIXED_CASE_POLICY_JSON,
    )
    # 스칼라 컬럼이 모두 NULL — dict 의 "value" 스칼라 fallback 이 새는 경로.
    _insert_run_feature(
        conn, run_id="mixedcase-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="mixedcase-hbm", feature_name="hbm_supply_tightness", value=None,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        value_json=_MIXED_CASE_POLICY_SCALAR_JSON,
    )
    # 서로 다른 케이스의 중복 정책 키 — 하나라도 차단이면 전부 억제돼야 한다.
    _insert_run_feature(
        conn, run_id="mixedcase-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="mixedcase-nand", feature_name="nand_wafer_direction", value=913.25,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        value_json=_DUPLICATE_CASE_POLICY_JSON,
    )
    # 대조군: 차단이 아닌 정책 값은 과차단 없이 스칼라를 유지한다.
    _insert_run_feature(
        conn, run_id="mixedcase-005930", symbol="005930",
        as_of="2026-08-01T18:00:00+09:00", cutoff="2026-08-01T16:00:00+09:00",
        feature_id="mixedcase-soxx", feature_name="soxx_adjusted_close", value=None,
        source_as_of="2026-08-01T07:00:00+00:00", ingested_at="2026-08-01T15:58:00+09:00",
        value_json=_MIXED_CASE_NON_BLOCKING_JSON,
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    monkeypatch.delenv("KSF_READ_USERNAME", raising=False)
    monkeypatch.delenv("KSF_READ_PASSWORD", raising=False)
    return TestClient(app)


def test_mixed_case_policy_key_suppresses_value_num(mixed_case_policy_client: TestClient):
    detail = mixed_case_policy_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["memory_indicators"]["dram"] == {
        "value": None,
        "status": "READY",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    assert "7433.25" not in detail.text


def test_mixed_case_policy_key_suppresses_value_text(mixed_case_policy_client: TestClient):
    detail = mixed_case_policy_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["memory_indicators"]["mu"] == {
        "value": None,
        "status": "READY",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    assert "mixedcase-directional-text" not in detail.text


def test_mixed_case_policy_key_suppresses_json_scalar_value(mixed_case_policy_client: TestClient):
    detail = mixed_case_policy_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["memory_indicators"]["hbm"] == {
        "value": None,
        "status": "READY",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    assert "6211.5" not in detail.text


def test_duplicate_cased_policy_keys_fail_closed_if_any_blocks(mixed_case_policy_client: TestClient):
    detail = mixed_case_policy_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["memory_indicators"]["nand"] == {
        "value": None,
        "status": "READY",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    assert "913.25" not in detail.text


def test_mixed_case_non_blocking_policy_value_preserves_scalar(mixed_case_policy_client: TestClient):
    """정책 키가 대소문자 변형이어도 값이 차단이 아니면 과차단하지 않는다."""
    detail = mixed_case_policy_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["global_environment"]["soxx"] == {
        "value": 351.75,
        "status": "READY",
        "unit": "USD",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }


@pytest.mark.parametrize("path", _KSF_ENDPOINTS)
def test_mixed_case_policy_ledger_leaks_nothing_on_any_endpoint(
    mixed_case_policy_client: TestClient, path: str
):
    response = mixed_case_policy_client.get(path, headers=_auth())
    assert response.status_code == 200
    text = response.text.lower()
    for needle in _MIXED_CASE_FORBIDDEN:
        assert needle not in text, needle


# --- 승인된 KSF B 리서치 데스크 계약: 공개 상태 taxonomy + has_restricted + 한국어 라벨 ---
#
# 우선순위: stale > missing > partial > restricted > ready. 빈 원장은 no_data 유지.
# has_restricted 는 차단 피처/차단 소스가 실재할 때 true 이되, 원시 taxonomy/정책/URL/
# 소스 라벨 억제 계약은 그대로 유지된다.

_STATE_AS_OF = "2026-08-01T18:00:00+09:00"
_STATE_CUTOFF = "2026-08-01T16:00:00+09:00"
_STATE_SOURCE_AS_OF = "2026-08-01T06:00:00+00:00"
_STATE_INGESTED = "2026-08-01T15:58:00+09:00"

_READY_FEATURES = (
    ("nvda_adjusted_close", 190.0, "READY"),
    ("foreign_net_flow", 500.0, "READY"),
)


def _make_state_ledger(
    db_path: Path,
    *,
    run_status: str,
    features: tuple[tuple[str, float | None, str], ...],
    blocked_source: bool = False,
) -> None:
    """단일 run 원장 — run_status/feature_status 조합으로 공개 상태 계약을 재현한다."""
    _make_ledger(db_path, populated=False)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO ksf_runs
           (run_id, symbol, trading_date, run_status, as_of_kst, available_data_cutoff)
           VALUES ('state-run', '005930', '2026-08-01', ?, ?, ?)""",
        (run_status, _STATE_AS_OF, _STATE_CUTOFF),
    )
    for index, (name, value, status) in enumerate(features):
        missing_reason = None
        if status in {"MISSING_OPTIONAL", "MISSING_REQUIRED", "LICENSE_BLOCKED"}:
            missing_reason = "state fixture"
        _insert_run_feature(
            conn, run_id="state-run", symbol="005930",
            as_of=_STATE_AS_OF, cutoff=_STATE_CUTOFF,
            feature_id=f"state-feature-{index}", feature_name=name, value=value,
            source_as_of=_STATE_SOURCE_AS_OF, ingested_at=_STATE_INGESTED,
            status=status, missing_reason=missing_reason,
        )
    if blocked_source:
        # 스키마가 허용하는 유일한 차단 소스 표현: BLOCKED_REVIEW + quality BLOCKED.
        conn.execute(
            """INSERT INTO ksf_collected_source_metadata
               (metadata_id, source_name, capture_date, quality_grade, raw_storage_policy)
               VALUES ('state-meta', 'memory_vendor_review', '2026-08-01', 'BLOCKED', 'NONE')"""
        )
        conn.execute(
            """INSERT INTO ksf_source_snapshots
               (snapshot_id, run_id, symbol, source_metadata_id, source_name,
                source_kind, source_status, source_as_of, ingested_at_kst,
                available_data_cutoff, quality_grade)
               VALUES ('state-snapshot', NULL, '005930', 'state-meta',
                       'memory_vendor_review', 'memory_license_review', 'BLOCKED_REVIEW',
                       ?, ?, ?, 'BLOCKED')""",
            (_STATE_SOURCE_AS_OF, _STATE_INGESTED, _STATE_CUTOFF),
        )
    conn.commit()
    conn.close()


def _state_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_status: str,
    features: tuple[tuple[str, float | None, str], ...],
    blocked_source: bool = False,
) -> TestClient:
    db_path = tmp_path / "state.sqlite3"
    _make_state_ledger(
        db_path, run_status=run_status, features=features, blocked_source=blocked_source
    )
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    monkeypatch.delenv("KSF_READ_USERNAME", raising=False)
    monkeypatch.delenv("KSF_READ_PASSWORD", raising=False)
    return TestClient(app)


_STATE_PRECEDENCE_CASES = [
    pytest.param(
        "READY", _READY_FEATURES,
        {"state": "ready", "has_stale": False, "has_missing": False, "has_restricted": False},
        id="ready",
    ),
    pytest.param(
        "PARTIAL_DATA", _READY_FEATURES,
        {"state": "partial", "has_missing": True, "has_restricted": False},
        id="partial-from-run-status",
    ),
    pytest.param(
        "READY", _READY_FEATURES + (("dram_price", None, "MISSING_OPTIONAL"),),
        {"state": "partial", "has_missing": True, "has_restricted": False},
        id="partial-from-missing-optional-feature",
    ),
    pytest.param(
        "MISSING_REQUIRED_DATA", _READY_FEATURES,
        {"state": "missing", "has_missing": True, "has_restricted": False},
        id="missing-from-run-status",
    ),
    pytest.param(
        "READY", _READY_FEATURES + (("institution_net_flow", None, "MISSING_REQUIRED"),),
        {"state": "missing", "has_missing": True, "has_restricted": False},
        id="missing-from-required-feature",
    ),
    pytest.param(
        "PARTIAL_DATA", _READY_FEATURES + (("institution_net_flow", None, "MISSING_REQUIRED"),),
        {"state": "missing", "has_missing": True, "has_restricted": False},
        id="missing-beats-partial",
    ),
    pytest.param(
        "READY", _READY_FEATURES + (("hbm_supply_tightness", None, "LICENSE_BLOCKED"),),
        {"state": "restricted", "has_missing": True, "has_restricted": True},
        id="restricted-only-blocked-feature",
    ),
    pytest.param(
        "PARTIAL_DATA", _READY_FEATURES + (("hbm_supply_tightness", None, "LICENSE_BLOCKED"),),
        {"state": "partial", "has_missing": True, "has_restricted": True},
        id="partial-beats-restricted",
    ),
    pytest.param(
        "STALE_DATA",
        _READY_FEATURES
        + (("institution_net_flow", None, "MISSING_REQUIRED"),
           ("hbm_supply_tightness", None, "LICENSE_BLOCKED")),
        {"state": "stale", "has_stale": True, "has_missing": True, "has_restricted": True},
        id="stale-wins-over-everything",
    ),
    pytest.param(
        "PARTIAL_DATA", _READY_FEATURES + (("dram_price", 2.0, "STALE"),),
        {"state": "stale", "has_stale": True, "has_restricted": False},
        id="stale-feature-beats-partial",
    ),
]


@pytest.mark.parametrize("run_status,features,expected", _STATE_PRECEDENCE_CASES)
def test_public_state_taxonomy_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    run_status: str, features: tuple, expected: dict,
):
    client = _state_client(tmp_path, monkeypatch, run_status=run_status, features=features)
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    quality = detail.json()["data_quality"]
    for key, value in expected.items():
        assert quality.get(key) == value, f"{key}: expected {value!r}, got {quality.get(key)!r}"


def test_blocked_source_only_headlines_restricted_without_faking_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """피처는 전부 READY 이고 차단 소스만 있으면 restricted — 결측을 지어내지 않는다."""
    client = _state_client(
        tmp_path, monkeypatch, run_status="READY", features=_READY_FEATURES,
        blocked_source=True,
    )
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    quality = detail.json()["data_quality"]
    assert quality["state"] == "restricted"
    assert quality.get("has_restricted") is True
    assert quality["has_missing"] is False
    assert quality["has_stale"] is False


def test_has_restricted_true_for_blocked_features_without_leaking_taxonomy(policy_client: TestClient):
    """has_restricted 는 진실을 말하되 원시 taxonomy/정책/URL 억제는 그대로 유지된다."""
    detail = policy_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["data_quality"].get("has_restricted") is True
    text = detail.text.lower()
    for needle in _POLICY_FORBIDDEN:
        assert needle not in text, needle


def test_empty_ledger_cards_keep_no_data_and_report_no_restricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "empty-restricted.sqlite3"
    _make_ledger(db_path, populated=False)
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    response = TestClient(app).get("/ksf/cards", headers=_auth())
    assert response.status_code == 200
    for card in response.json()["cards"]:
        assert card["data_quality"]["state"] == "no_data"
        assert card["data_quality"]["has_missing"] is True
        assert card["data_quality"].get("has_restricted") is False


def test_base_fixture_headlines_partial_with_restricted_qualifier(client: TestClient):
    """프로덕션 재현 fixture: PARTIAL_DATA 종목은 raw missing 이 아니라 partial 을
    headline 하고, 차단 항목/결측 진실(has_restricted/has_missing)은 유지한다."""
    cards = {
        card["symbol"]: card["data_quality"]
        for card in client.get("/ksf/cards", headers=_auth()).json()["cards"]
    }
    hynix = cards["000660"]  # PARTIAL_DATA run + LICENSE_BLOCKED memory_license 행
    assert hynix["state"] == "partial"
    assert hynix["has_stale"] is False
    assert hynix["has_missing"] is True
    assert hynix.get("has_restricted") is True
    samsung = cards["005930"]  # STALE_DATA run — stale 이 항상 우선
    assert samsung["state"] == "stale"
    assert samsung.get("has_restricted") is True


# --- B 리서치 데스크 HTML: 한국어 공개 라벨, coverage 그룹, read-only, 양방향 탐색 ---

_B_COVERAGE_GROUPS = ("핵심 수급", "글로벌 환경", "메모리 지표")


@pytest.mark.parametrize("path", ["/ksf", "/ksf/005930", "/ksf/000660"])
def test_research_desk_html_shows_coverage_groups_and_read_only_posture(
    client: TestClient, path: str
):
    assert client.get(path).status_code == 401  # 인증은 그대로 필수
    response = client.get(path, headers=_auth())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for group in _B_COVERAGE_GROUPS:
        assert group in response.text, group
    # read-only 성격이 화면에 명시돼야 한다.
    assert "읽기 전용" in response.text
    lowered = response.text.lower()
    assert "<form" not in lowered
    assert "<button" not in lowered
    assert 'method="post"' not in lowered
    assert not any(word in lowered for word in FORBIDDEN)


def test_research_desk_html_uses_korean_state_labels_not_raw_missing(client: TestClient):
    listing = client.get("/ksf", headers=_auth())
    assert listing.status_code == 200
    assert "일부 데이터 준비" in listing.text   # 000660: PARTIAL_DATA primary 상태
    assert "업데이트 필요" in listing.text      # 005930: stale 우선
    assert "사용 제한" in listing.text          # secondary qualifier
    assert "데이터 상태: missing" not in listing.text
    detail = client.get("/ksf/000660", headers=_auth())
    assert detail.status_code == 200
    assert "일부 데이터 준비" in detail.text
    assert "데이터 상태: missing" not in detail.text


def test_research_desk_html_navigates_between_both_symbols(client: TestClient):
    listing = client.get("/ksf", headers=_auth())
    assert 'href="/ksf/005930"' in listing.text
    assert 'href="/ksf/000660"' in listing.text
    samsung = client.get("/ksf/005930", headers=_auth())
    assert 'href="/ksf/000660"' in samsung.text
    assert 'href="/ksf"' in samsung.text
    hynix = client.get("/ksf/000660", headers=_auth())
    assert 'href="/ksf/005930"' in hynix.text
    assert 'href="/ksf"' in hynix.text


def test_research_desk_html_distinguishes_ready_pending_restricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """준비됨/수집 대기/사용 제한을 텍스트로 구분한다 — 공백이 앱 오류처럼 보이면 안 된다."""
    client = _state_client(
        tmp_path, monkeypatch, run_status="PARTIAL_DATA",
        features=(
            ("nvda_adjusted_close", 190.0, "READY"),
            ("hbm_supply_tightness", None, "LICENSE_BLOCKED"),
        ),
    )
    page = client.get("/ksf/005930", headers=_auth())
    assert page.status_code == 200
    assert "준비됨" in page.text       # READY 항목
    assert "수집 대기" in page.text    # 아직 수집되지 않은 항목
    assert "사용 제한" in page.text    # 차단 항목 — 내부 taxonomy 비노출
    assert "license_blocked" not in page.text.lower()


_HEADLINE_LABEL_CASES = [
    pytest.param("READY", _READY_FEATURES, "필수 데이터 준비", id="ready-headline"),
    pytest.param("PARTIAL_DATA", _READY_FEATURES, "일부 데이터 준비", id="partial-headline"),
    pytest.param("MISSING_REQUIRED_DATA", _READY_FEATURES, "필수 데이터 부족", id="missing-headline"),
    pytest.param("STALE_DATA", _READY_FEATURES, "업데이트 필요", id="stale-headline"),
    pytest.param(
        "READY", _READY_FEATURES + (("hbm_supply_tightness", None, "LICENSE_BLOCKED"),),
        "사용 제한", id="restricted-headline",
    ),
]


@pytest.mark.parametrize("run_status,features,label", _HEADLINE_LABEL_CASES)
def test_research_desk_headline_uses_public_korean_taxonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    run_status: str, features: tuple, label: str,
):
    client = _state_client(tmp_path, monkeypatch, run_status=run_status, features=features)
    page = client.get("/ksf/005930", headers=_auth())
    assert page.status_code == 200
    assert label in page.text
    # raw 상태 문자열을 headline 으로 노출하지 않는다 (특히 missing).
    assert "데이터 상태: missing" not in page.text


# --- Codex 리뷰 잔여 이슈 회귀 ---------------------------------------------------
# 블로커 1: 스키마 유효 차단 소스 taxonomy(BLOCKED_REVIEW / quality BLOCKED)는
#           JSON/HTML 어디에도 원문으로 실리지 않고 UNAVAILABLE 로 일반화된다.
# 블로커 2: 단위 의미론 — 안전 canonical 단위 allowlist + 보수적 이름 fallback,
#           미허용/악성 단위 비노출, HTML 단위 라벨/단위 미제공/비유한수 처리.
# 비차단: KST 변환 표기, href/fragment URL 인코딩, 모바일 overflow 방지 CSS.


def _rows_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict],
) -> TestClient:
    """빈 원장에 지정한 피처 행들만 넣은 단일 run 클라이언트 (단위/값 회귀용)."""
    db_path = tmp_path / "rows.sqlite3"
    _make_ledger(db_path, populated=False)
    conn = sqlite3.connect(db_path)
    for index, row in enumerate(rows):
        _insert_run_feature(
            conn, run_id="rows-run", symbol="005930",
            as_of=_STATE_AS_OF, cutoff=_STATE_CUTOFF,
            feature_id=f"rows-{index}", source_as_of=_STATE_SOURCE_AS_OF,
            ingested_at=_STATE_INGESTED, **row,
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    monkeypatch.delenv("KSF_READ_USERNAME", raising=False)
    monkeypatch.delenv("KSF_READ_PASSWORD", raising=False)
    return TestClient(app)


# --- 블로커 1: 차단 소스 taxonomy JSON 억제 ---


def test_public_status_generalizes_blocked_markers_case_insensitively():
    from ksf import web_api

    for raw in ("BLOCKED", "BLOCKED_REVIEW", "Blocked_Review", " blocked ", "bLoCkEd_ReViEw"):
        assert web_api._public_status(raw) == "UNAVAILABLE", raw
    # 정상 상태/등급은 과차단하지 않는다.
    assert web_api._public_status("COLLECTED") == "COLLECTED"
    assert web_api._public_status("CLOSE_CONFIRMED") == "CLOSE_CONFIRMED"
    assert web_api._public_status("A") == "A"
    assert web_api._public_status(None) is None


def test_schema_valid_blocked_source_is_publicized_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """BLOCKED_REVIEW/quality BLOCKED 는 감지(has_restricted)뿐 아니라 직렬화에서도
    UNAVAILABLE 로 일반화된다."""
    client = _state_client(
        tmp_path, monkeypatch, run_status="READY", features=_READY_FEATURES,
        blocked_source=True,
    )
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    quality = detail.json()["data_quality"]
    assert quality.get("has_restricted") is True
    assert quality["source_statuses"] == [{
        "source": "memory_vendor_review",
        "status": "UNAVAILABLE",
        "quality": "UNAVAILABLE",
        "source_as_of": _STATE_SOURCE_AS_OF,
    }]


@pytest.mark.parametrize("path", _KSF_ENDPOINTS)
def test_blocked_source_taxonomy_absent_from_every_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
):
    client = _state_client(
        tmp_path, monkeypatch, run_status="READY", features=_READY_FEATURES,
        blocked_source=True,
    )
    response = client.get(path, headers=_auth())
    assert response.status_code == 200
    # BLOCKED / BLOCKED_REVIEW 어느 표기도 (대소문자 무관) 실리면 안 된다.
    assert "blocked" not in response.text.lower()


def test_mixed_case_blocked_source_markers_fail_closed_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "rogue-blocked.sqlite3"
    _make_state_ledger(db_path, run_status="READY", features=_READY_FEATURES)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        """INSERT INTO ksf_collected_source_metadata
           (metadata_id, source_name, capture_date, quality_grade, raw_storage_policy)
           VALUES ('rogue-meta', 'memory_vendor_review', '2026-08-01', 'bLoCkEd', 'NONE')"""
    )
    conn.execute(
        """INSERT INTO ksf_source_snapshots
           (snapshot_id, run_id, symbol, source_metadata_id, source_name,
            source_kind, source_status, source_as_of, ingested_at_kst,
            available_data_cutoff, quality_grade)
           VALUES ('rogue-snapshot', NULL, '005930', 'rogue-meta',
                   'memory_vendor_review', 'memory_license_review', 'Blocked_Review',
                   ?, ?, ?, ' blocked ')""",
        (_STATE_SOURCE_AS_OF, _STATE_INGESTED, _STATE_CUTOFF),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    client = TestClient(app)
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    quality = detail.json()["data_quality"]
    assert quality.get("has_restricted") is True
    assert quality["source_statuses"] == [{
        "source": "memory_vendor_review",
        "status": "UNAVAILABLE",
        "quality": "UNAVAILABLE",
        "source_as_of": _STATE_SOURCE_AS_OF,
    }]
    for path in _KSF_ENDPOINTS:
        response = client.get(path, headers=_auth())
        assert response.status_code == 200
        assert "blocked" not in response.text.lower(), path


# --- 블로커 2: 단위 의미론 (JSON) ---


_UNIT_FALLBACK_CASES = [
    pytest.param("foreigner_net_buy_quantity", "domestic_flow", "foreign", "shares", id="foreign-qty-shares"),
    pytest.param("institution_total_net_buy_quantity", "domestic_flow", "institution", "shares", id="institution-qty-shares"),
    pytest.param("individual_net_buy_quantity", "domestic_flow", "individual", "shares", id="individual-qty-shares"),
    pytest.param("foreign_net_buy_value", "domestic_flow", "foreign", "KRW", id="net-buy-value-krw"),
    pytest.param("nvda_adjusted_close", "global_environment", "nvda", "USD", id="nvda-usd"),
    pytest.param("soxx_close", "global_environment", "soxx", "USD", id="soxx-usd"),
    pytest.param("mu_close", "memory_indicators", "mu", "USD", id="mu-usd"),
    pytest.param("tsm_twse_close", "global_environment", "tsm", "TWD", id="tsm-twse-twd"),
    pytest.param("usdkrw", "global_environment", "usdkrw", "KRW/USD", id="usdkrw-fx"),
]


@pytest.mark.parametrize("feature_name,group,key,expected_unit", _UNIT_FALLBACK_CASES)
def test_null_ledger_unit_uses_conservative_name_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    feature_name: str, group: str, key: str, expected_unit: str,
):
    client = _rows_client(tmp_path, monkeypatch, [{"feature_name": feature_name, "value": 123.0}])
    item = client.get("/ksf/cards/005930", headers=_auth()).json()[group][key]
    assert item["value"] == 123.0
    assert item.get("unit") == expected_unit


@pytest.mark.parametrize("feature_name,group,key", [
    pytest.param("foreign_net_flow", "domestic_flow", "foreign", id="ambiguous-net-flow"),
    pytest.param("tsm_adjusted_close", "global_environment", "tsm", id="ambiguous-tsm-adr"),
    pytest.param("dram_price", "memory_indicators", "dram", id="no-guess-dram"),
    pytest.param("nand_price", "memory_indicators", "nand", id="no-guess-nand"),
    pytest.param("hbm_supply_tightness", "memory_indicators", "hbm", id="no-guess-hbm"),
])
def test_no_unit_is_guessed_for_ambiguous_features(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    feature_name: str, group: str, key: str,
):
    client = _rows_client(tmp_path, monkeypatch, [{"feature_name": feature_name, "value": 42.0}])
    item = client.get("/ksf/cards/005930", headers=_auth()).json()[group][key]
    assert item["value"] == 42.0
    assert item.get("unit") is None


def test_valid_ledger_unit_is_preferred_and_canonicalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _rows_client(tmp_path, monkeypatch, [
        # fallback 이 없는 지표에 원장 단위가 있으면 그대로 공개한다.
        {"feature_name": "dram_price", "value": 2.1, "unit": "USD"},
        # 원장 단위가 유효하면 이름 fallback(USD)보다 우선한다 + 대소문자/공백 정규화.
        {"feature_name": "nvda_adjusted_close", "value": 190.0, "unit": " twd "},
    ])
    card = client.get("/ksf/cards/005930", headers=_auth()).json()
    assert card["memory_indicators"]["dram"].get("unit") == "USD"
    assert card["global_environment"]["nvda"].get("unit") == "TWD"


def test_unknown_or_malicious_ledger_unit_never_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _rows_client(tmp_path, monkeypatch, [
        {"feature_name": "nvda_adjusted_close", "value": 190.0,
         "unit": '악성단위<script>alert("x")</script>'},
        {"feature_name": "soxx_adjusted_close", "value": 300.0, "unit": "KRW억"},
    ])
    card = client.get("/ksf/cards/005930", headers=_auth()).json()
    # allowlist 밖 단위는 None — 이름 fallback 으로도 대체하지 않는다(행 자체가 의심).
    assert card["global_environment"]["nvda"].get("unit") is None
    assert card["global_environment"]["soxx"].get("unit") is None
    for path in _KSF_ENDPOINTS:
        text = client.get(path, headers=_auth()).text.lower()
        assert "악성단위" not in text, path
        assert "krw억" not in text, path
        assert '<script>alert' not in text, path


def test_restricted_value_hides_unit_even_with_ledger_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _rows_client(tmp_path, monkeypatch, [
        {"feature_name": "hbm_supply_tightness", "value": None, "unit": "USD",
         "status": "LICENSE_BLOCKED", "missing_reason": "license review"},
    ])
    hbm = client.get("/ksf/cards/005930", headers=_auth()).json()["memory_indicators"]["hbm"]
    # 차단 항목은 값뿐 아니라 단위도 노출하지 않는다 (기존 계약 dict 그대로).
    assert hbm == {
        "value": None,
        "status": "UNAVAILABLE",
        "source_as_of": _STATE_SOURCE_AS_OF,
    }


def test_nonfinite_ledger_value_is_null_in_json_and_dash_in_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _rows_client(tmp_path, monkeypatch, [
        {"feature_name": "nvda_adjusted_close", "value": float("inf")},
    ])
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    nvda = detail.json()["global_environment"]["nvda"]
    assert nvda["value"] is None
    assert nvda.get("unit") is None
    page = client.get("/ksf/005930", headers=_auth())
    assert page.status_code == 200
    assert "—" in page.text
    assert "inf" not in page.text.lower()


def test_fmt_value_nonfinite_renders_dash():
    from dashboard.app import _fmt_value

    for value in (float("inf"), float("-inf"), float("nan")):
        assert _fmt_value(value) == "—"
        assert _fmt_value(value, signed=True) == "—"


# --- 블로커 2: 단위 의미론 (HTML) ---


def test_html_renders_safe_unit_labels_and_neutral_domestic_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _rows_client(tmp_path, monkeypatch, [
        {"feature_name": "foreigner_net_buy_quantity", "value": 1200.0},
        {"feature_name": "individual_net_buy_quantity", "value": -1500.0},
        {"feature_name": "nvda_adjusted_close", "value": 190.0},
        {"feature_name": "tsm_twse_close", "value": 1010.0},
        {"feature_name": "usdkrw", "value": 1382.5},
        {"feature_name": "dram_price", "value": 2.1},
    ])
    page = client.get("/ksf/005930", headers=_auth())
    assert page.status_code == 200
    # 국내 수급 라벨은 단위 중립 표기.
    assert "외국인 순수급" in page.text
    assert "기관 순수급" in page.text
    assert "개인 순수급" in page.text
    assert "순유입" not in page.text
    # 안전 단위만 명시적 한국어/통화 라벨로 렌더링한다.
    assert '<span class="unit">주</span>' in page.text
    assert '<span class="unit">USD</span>' in page.text
    assert '<span class="unit">TWD</span>' in page.text
    assert '<span class="unit">원/USD</span>' in page.text
    # 숫자값인데 안전 단위가 없으면 단위를 암시하지 않고 명시적으로 알린다.
    assert "단위 미제공" in page.text
    # 부호/천단위 표기는 유지된다.
    assert "+1,200" in page.text
    assert "-1,500" in page.text


# --- 비차단 3: KST 시각 표기 ---


def test_fmt_ts_converts_mixed_offsets_to_kst_with_single_label():
    from dashboard.app import _fmt_ts

    assert _fmt_ts("2026-08-01T06:00:00+00:00") == "2026.08.01 15:00 KST"
    assert _fmt_ts("2026-08-01T18:00:00+09:00") == "2026.08.01 18:00 KST"
    # 날짜 경계를 넘는 변환도 정확해야 한다.
    assert _fmt_ts("2026-08-01T23:30:00+00:00") == "2026.08.02 08:30 KST"
    assert _fmt_ts("2026-08-01T06:00:00+00:00", full=False) == "08.01 15:00 KST"


def test_fmt_ts_naive_or_unparseable_is_not_labeled_kst():
    from dashboard.app import _fmt_ts

    assert _fmt_ts("2026-08-01T15:30:00") == "2026.08.01 15:30"
    assert _fmt_ts("2026-08-01") == "2026.08.01"
    assert _fmt_ts("not-a-timestamp") == "not-a-timestamp"
    assert _fmt_ts(None) == "—"
    for raw in ("2026-08-01T15:30:00", "2026-08-01", "not-a-timestamp"):
        assert "KST" not in _fmt_ts(raw), raw


def test_metric_source_ts_is_escaped_when_unparseable():
    from dashboard.app import _metric_html

    out = _metric_html(
        {"x": {"value": 1.0, "status": "READY", "source_as_of": "<img src=x onerror=y>"}},
        "x", "라벨", signed=False,
    )
    assert "<img" not in out
    assert "&lt;img" in out
    assert "KST" not in out


def test_detail_page_displays_kst_converted_times_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _state_client(tmp_path, monkeypatch, run_status="READY", features=_READY_FEATURES)
    page = client.get("/ksf/005930", headers=_auth())
    assert page.status_code == 200
    assert "2026.08.01 18:00 KST" in page.text   # as_of +09:00
    assert "2026.08.01 16:00 KST" in page.text   # cutoff +09:00
    assert "2026.08.01 15:00 KST" in page.text   # source 06:00+00:00 → KST 변환
    assert "08.01 15:00 KST" in page.text        # 메트릭 축약형
    assert "2026.08.01 06:00" not in page.text   # UTC 시각을 그대로 표기하지 않는다
    assert "KST KST" not in page.text            # KST 라벨은 시각당 정확히 1회


# --- 비차단 4: href/fragment URL 인코딩 ---


def test_symbol_href_helper_encodes_adversarial_symbols():
    from dashboard.app import _symbol_href

    # 지원 심볼은 기존 경로 그대로.
    assert _symbol_href("005930") == "/ksf/005930"
    assert _symbol_href("005930", fragment=True) == "#sym-005930"
    # 속성 컨텍스트를 깨는 문자는 전부 percent 인코딩된다.
    assert _symbol_href('00"59<b>&30') == "/ksf/00%2259%3Cb%3E%2630"
    assert _symbol_href("a/b?c#d", fragment=True) == "#sym-a%2Fb%3Fc%23d"


def test_symbol_link_attribute_context_is_encoded_and_escaped():
    from dashboard.app import _symbol_href, _symbol_link

    card = {"symbol": '00"59<b>', "name": '<i>이름</i>"quote'}
    out = _symbol_link(card, _symbol_href(card["symbol"]))
    assert 'href="/ksf/00%2259%3Cb%3E"' in out
    assert "<i>" not in out and "<b>" not in out
    href_value = out.split('href="', 1)[1].split('"', 1)[0]
    assert "<" not in href_value and '"' not in href_value


# --- 비차단 5: 모바일 overflow 방지 CSS + 긴 무공백 원장 값 escape ---


def test_metric_grid_css_prevents_mobile_overflow(client: TestClient):
    page = client.get("/ksf/005930", headers=_auth())
    assert page.status_code == 200
    metric_block = re.search(r"\.metric\{[^}]*\}", page.text)
    assert metric_block is not None
    assert "min-width:0" in metric_block.group(0)
    assert "overflow-wrap:anywhere" in metric_block.group(0)
    card_block = re.search(r"\.card\{[^}]*\}", page.text)
    assert card_block is not None
    assert "min-width:0" in card_block.group(0)
    rec_block = re.search(r"\.rec\{[^}]*\}", page.text)
    assert rec_block is not None
    assert "min-width:0" in rec_block.group(0)
    nav_link_block = re.search(r"nav a\{[^}]*\}", page.text)
    assert nav_link_block is not None
    assert "min-height:44px" in nav_link_block.group(0)
    assert "inline-flex" in nav_link_block.group(0)


def test_long_unbroken_markup_ledger_value_is_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payload = "무공백" + "x" * 180 + '<script>alert("주입")</script>' + "y" * 40
    client = _rows_client(tmp_path, monkeypatch, [
        {"feature_name": "dram_spot_direction", "value": None, "value_text": payload},
    ])
    page = client.get("/ksf/005930", headers=_auth())
    assert page.status_code == 200
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;alert(&quot;주입&quot;)&lt;/script&gt;" in page.text
    assert "x" * 180 in page.text


# --- Codex 재리뷰 블로커: 통합 사설 marker predicate (BLOCKED / BLOCKED_REVIEW) ---
#
# 하나의 case-insensitive predicate 가 기존 정책/URL/라이선스 마커에 더해
# 사설 차단 토큰(BLOCKED, BLOCKED_REVIEW — 정확한 상태값 또는 구분자로 분리된
# 토큰)을 모든 원장 유래 경로(run_status, feature_status, source name/status/
# quality, AI 설명)에서 감지해야 한다. "unblocked" 같은 일반 단어에는 절대
# 반응하지 않는다 (false-positive 대조군).

# 영숫자에 인접하지 않은 "blocked" 토큰 — "unblocked" 는 매칭되지 않는다.
_DELIMITED_BLOCKED_RE = re.compile(r"(?<![0-9a-z])blocked")


def _assert_no_delimited_blocked_token(text: str) -> None:
    lowered = text.lower()
    match = _DELIMITED_BLOCKED_RE.search(lowered)
    assert match is None, f"delimited blocked token leaked: ...{lowered[max(0, match.start() - 40):match.end() + 40]}..."


def test_private_marker_predicate_matches_tokens_not_unblocked():
    from ksf import web_api

    predicate = web_api._has_private_marker
    # 정확한 상태값 + 대소문자/공백 변형.
    for raw in ("BLOCKED", "BLOCKED_REVIEW", "blocked", " Blocked_Review ", "bLoCkEd"):
        assert predicate(raw) is True, raw
    # 문자열/소스명 내부의 구분자로 분리된 토큰.
    for raw in (
        "memory_vendor_blocked_feed",
        "BLOCKED_REVIEW pending",
        "차단(BLOCKED) 항목",
        "quality=blocked;vendor",
    ):
        assert predicate(raw) is True, raw
    # 기존 금지 마커도 같은 predicate 하나로 감지된다.
    for raw in ("LICENSE_BLOCKED", "Display_Policy", "https://vendor.example", "www.evil.example"):
        assert predicate(raw) is True, raw
    # 일반 단어/정상 상태에는 반응하지 않는다.
    for raw in ("unblocked", "UNBLOCKED", "unblocked_vendor_feed", "READY",
                "CLOSE_CONFIRMED", "COLLECTED", "A", ""):
        assert predicate(raw) is False, raw
    assert predicate(None) is False
    assert predicate(3.5) is False


def test_private_marker_schema_valid_blocked_review_run_is_restricted_and_generalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """스키마 유효 run_status=BLOCKED_REVIEW: 공개 상태는 restricted, run_status 는
    UNAVAILABLE 로 일반화, has_restricted=true, 결측/신선도 진실은 유지된다."""
    client = _state_client(
        tmp_path, monkeypatch, run_status="BLOCKED_REVIEW", features=_READY_FEATURES
    )
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    card = detail.json()
    quality = card["data_quality"]
    assert quality["state"] == "restricted"
    assert quality.get("has_restricted") is True
    assert quality["has_missing"] is False   # READY 피처뿐 — 결측을 지어내지 않는다
    assert quality["has_stale"] is False
    assert quality["run_status"] == "UNAVAILABLE"
    # run 차단은 개별 READY 피처 값을 과억제하지 않는다.
    assert card["global_environment"]["nvda"]["value"] == 190.0
    assert card["global_environment"]["nvda"]["status"] == "READY"


@pytest.mark.parametrize("path", _KSF_ENDPOINTS)
def test_private_marker_blocked_review_run_absent_from_every_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
):
    client = _state_client(
        tmp_path, monkeypatch, run_status="BLOCKED_REVIEW", features=_READY_FEATURES
    )
    response = client.get(path, headers=_auth())
    assert response.status_code == 200
    _assert_no_delimited_blocked_token(response.text)


_RUN_RESTRICTION_PRECEDENCE_CASES = [
    pytest.param(
        ("dram_price", 2.0, "STALE"), "stale",
        id="stale-beats-run-restriction",
    ),
    pytest.param(
        ("institution_net_flow", None, "MISSING_REQUIRED"), "missing",
        id="required-missing-beats-run-restriction",
    ),
    pytest.param(
        ("dram_price", None, "MISSING_OPTIONAL"), "partial",
        id="partial-beats-run-restriction",
    ),
]


@pytest.mark.parametrize("extra_feature,expected_state", _RUN_RESTRICTION_PRECEDENCE_CASES)
def test_private_marker_run_restriction_yields_to_state_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    extra_feature: tuple, expected_state: str,
):
    """restricted 는 stale/필수 결측/partial 보다 후순위 — has_restricted 는 그대로 참."""
    client = _state_client(
        tmp_path, monkeypatch, run_status="BLOCKED_REVIEW",
        features=_READY_FEATURES + (extra_feature,),
    )
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    quality = detail.json()["data_quality"]
    assert quality["state"] == expected_state
    assert quality.get("has_restricted") is True
    assert quality["run_status"] == "UNAVAILABLE"


def _rogue_blocked_feature_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str,
) -> TestClient:
    """스키마 CHECK 를 우회해 rogue 차단 feature_status(+살아있는 값/단위)를 주입한다."""
    db_path = tmp_path / "rogue-blocked-feature.sqlite3"
    _make_state_ledger(db_path, run_status="READY", features=_READY_FEATURES)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    _insert_run_feature(
        conn, run_id="state-run", symbol="005930",
        as_of=_STATE_AS_OF, cutoff=_STATE_CUTOFF,
        feature_id="rogue-blocked-hbm", feature_name="hbm_supply_tightness",
        value=4321.75, source_as_of=_STATE_SOURCE_AS_OF, ingested_at=_STATE_INGESTED,
        status=status, unit="USD",
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    monkeypatch.delenv("KSF_READ_USERNAME", raising=False)
    monkeypatch.delenv("KSF_READ_PASSWORD", raising=False)
    return TestClient(app)


@pytest.mark.parametrize("status", ["BLOCKED", "BLOCKED_REVIEW", "bLoCkEd", "Blocked_Review"])
def test_private_marker_rogue_blocked_feature_suppresses_value_and_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
):
    """rogue/대소문자 변형 차단 feature_status: 값·단위 전부 억제 + UNAVAILABLE 일반화 +
    has_restricted 참 + 억제된 항목의 has_missing 진실 유지."""
    client = _rogue_blocked_feature_client(tmp_path, monkeypatch, status=status)
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    card = detail.json()
    # 단위 키까지 포함해 완전 일치 — 값/단위가 어떤 형태로도 노출되면 안 된다.
    assert card["memory_indicators"]["hbm"] == {
        "value": None,
        "status": "UNAVAILABLE",
        "source_as_of": _STATE_SOURCE_AS_OF,
    }
    quality = card["data_quality"]
    assert quality.get("has_restricted") is True
    # 값이 억제된 차단 항목은 공개 화면 기준으로 결측이 맞다 — 거짓 ready 금지.
    assert quality["has_missing"] is True
    assert quality["state"] == "restricted"
    assert "4321.75" not in detail.text
    _assert_no_delimited_blocked_token(detail.text)


@pytest.mark.parametrize("path", _KSF_ENDPOINTS)
def test_private_marker_rogue_blocked_feature_absent_from_every_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
):
    client = _rogue_blocked_feature_client(tmp_path, monkeypatch, status="bLoCkEd_ReViEw")
    response = client.get(path, headers=_auth())
    assert response.status_code == 200
    assert "4321.75" not in response.text
    _assert_no_delimited_blocked_token(response.text)


def _named_source_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_name: str,
    quality_grade: str = "B",
) -> TestClient:
    """스키마 유효(CLOSE_CONFIRMED) 소스 스냅샷을 임의 source_name 으로 주입한다."""
    db_path = tmp_path / "named-source.sqlite3"
    _make_state_ledger(db_path, run_status="READY", features=_READY_FEATURES)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO ksf_collected_source_metadata
           (metadata_id, source_name, capture_date, quality_grade, raw_storage_policy)
           VALUES ('named-meta', ?, '2026-08-01', ?, 'NONE')""",
        (source_name, quality_grade),
    )
    conn.execute(
        """INSERT INTO ksf_source_snapshots
           (snapshot_id, run_id, symbol, source_metadata_id, source_name,
            source_kind, source_status, source_as_of, ingested_at_kst,
            available_data_cutoff, quality_grade)
           VALUES ('named-snapshot', NULL, '005930', 'named-meta', ?,
                   'memory_license_review', 'CLOSE_CONFIRMED', ?, ?, ?, ?)""",
        (source_name, _STATE_SOURCE_AS_OF, _STATE_INGESTED, _STATE_CUTOFF, quality_grade),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    monkeypatch.delenv("KSF_READ_USERNAME", raising=False)
    monkeypatch.delenv("KSF_READ_PASSWORD", raising=False)
    return TestClient(app)


def test_private_marker_delimited_blocked_source_name_masked_and_restricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """구분자로 분리된 blocked 토큰을 담은 source_name 은 기존 일반 라벨로 대체되고
    has_restricted 에 포함된다. 행 자체(상태/품질/시각)는 보존한다."""
    client = _named_source_client(
        tmp_path, monkeypatch, source_name="memory_vendor_blocked_feed"
    )
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    quality = detail.json()["data_quality"]
    assert quality["source_statuses"] == [{
        "source": "restricted_source",
        "status": "CLOSE_CONFIRMED",
        "quality": "B",
        "source_as_of": _STATE_SOURCE_AS_OF,
    }]
    assert quality.get("has_restricted") is True
    assert quality["state"] == "restricted"
    assert "memory_vendor_blocked_feed" not in detail.text
    for path in _KSF_ENDPOINTS:
        response = client.get(path, headers=_auth())
        assert response.status_code == 200
        _assert_no_delimited_blocked_token(response.text)


def test_private_marker_unblocked_source_name_stays_public_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """false-positive 대조군: "unblocked" 를 담은 정상 source_name 은 그대로 공개되고
    단독으로는 어떤 제한도 유발하지 않는다."""
    client = _named_source_client(
        tmp_path, monkeypatch, source_name="unblocked_vendor_feed", quality_grade="A"
    )
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    quality = detail.json()["data_quality"]
    assert quality["source_statuses"] == [{
        "source": "unblocked_vendor_feed",
        "status": "CLOSE_CONFIRMED",
        "quality": "A",
        "source_as_of": _STATE_SOURCE_AS_OF,
    }]
    assert quality.get("has_restricted") is False
    assert quality["state"] == "ready"


_PM_SAFE_DRIVER = "외국인 수급 우호적"
_PM_SAFE_RISK = "메모리 지표 변동성 확대"
_PM_UNBLOCKED_RISK = "unblocked 지표는 정상 반영"
_PM_BLOCKED_DRIVERS = [
    _PM_SAFE_DRIVER,
    "BLOCKED_REVIEW 대기 항목 제외",
    "vendor_blocked 소스 제외",
]
_PM_BLOCKED_RISKS = [
    _PM_SAFE_RISK,
    "차단(BLOCKED) 데이터 미반영",
    _PM_UNBLOCKED_RISK,
]


def _insert_state_ai_explanations(db_path: Path, drivers: list[str], risks: list[str]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO ksf_ai_requests
           (ai_request_id, run_id, symbol, purpose, as_of_kst,
            available_data_cutoff, prompt_template_version,
            redaction_policy_version, input_ledger_hash_sha256,
            prompt_hash_sha256, model_provider, model_name)
           VALUES ('pm-request', 'state-run', '005930', 'explain_decision',
                   ?, ?, 'v1', 'v1', 'pm-input', 'pm-prompt', 'offline', 'fixture')""",
        (_STATE_AS_OF, _STATE_CUTOFF),
    )
    conn.execute(
        """INSERT INTO ksf_ai_responses
           (ai_response_id, ai_request_id, run_id, symbol, response_status,
            response_hash_sha256, summary, drivers_json, risks_json,
            model_provider, model_name)
           VALUES ('pm-response', 'pm-request', 'state-run', '005930', 'OK',
                   'pm-hash', '단일 종목 흐름 요약', ?, ?, 'offline', 'fixture')""",
        (json.dumps(drivers, ensure_ascii=False), json.dumps(risks, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def test_private_marker_ai_explanations_dropped_and_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """차단 토큰을 담은 AI driver/risk 문자열은 통째로 폐기되고 has_restricted 를
    참으로 만든다. "unblocked" 대조군 문자열은 살아남는다."""
    db_path = tmp_path / "pm-ai.sqlite3"
    _make_state_ledger(db_path, run_status="READY", features=_READY_FEATURES)
    _insert_state_ai_explanations(db_path, _PM_BLOCKED_DRIVERS, _PM_BLOCKED_RISKS)
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    monkeypatch.delenv("KSF_READ_USERNAME", raising=False)
    monkeypatch.delenv("KSF_READ_PASSWORD", raising=False)
    client = TestClient(app)
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    card = detail.json()
    assert card["evidence"] == [_PM_SAFE_DRIVER]
    assert card["counterarguments"] == [_PM_SAFE_RISK, _PM_UNBLOCKED_RISK]
    assert card["data_quality"].get("has_restricted") is True
    assert card["data_quality"]["state"] == "restricted"
    for path in _KSF_ENDPOINTS:
        response = client.get(path, headers=_auth())
        assert response.status_code == 200
        _assert_no_delimited_blocked_token(response.text)
        assert "vendor_blocked" not in response.text.lower()


def test_private_marker_combined_adversarial_ledger_leaks_nothing_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """총력전: BLOCKED_REVIEW run + rogue 차단 피처 + blocked 토큰 소스명 + 차단 AI 설명
    + unblocked 대조군 소스를 한 원장에 모두 넣어도 4개 엔드포인트 어디에도 구분된
    blocked 토큰이 없고, 대조군은 과차단되지 않는다."""
    db_path = tmp_path / "pm-combined.sqlite3"
    _make_state_ledger(db_path, run_status="BLOCKED_REVIEW", features=_READY_FEATURES)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    _insert_run_feature(
        conn, run_id="state-run", symbol="005930",
        as_of=_STATE_AS_OF, cutoff=_STATE_CUTOFF,
        feature_id="pm-rogue-hbm", feature_name="hbm_supply_tightness",
        value=4321.75, source_as_of=_STATE_SOURCE_AS_OF, ingested_at=_STATE_INGESTED,
        status="Blocked_Review", unit="USD",
    )
    for suffix, source_name, grade in (
        ("masked", "memory_vendor_blocked_feed", "B"),
        ("control", "unblocked_vendor_feed", "A"),
    ):
        conn.execute(
            """INSERT INTO ksf_collected_source_metadata
               (metadata_id, source_name, capture_date, quality_grade, raw_storage_policy)
               VALUES (?, ?, '2026-08-01', ?, 'NONE')""",
            (f"pm-meta-{suffix}", source_name, grade),
        )
        conn.execute(
            """INSERT INTO ksf_source_snapshots
               (snapshot_id, run_id, symbol, source_metadata_id, source_name,
                source_kind, source_status, source_as_of, ingested_at_kst,
                available_data_cutoff, quality_grade)
               VALUES (?, NULL, '005930', ?, ?, 'memory_license_review',
                       'CLOSE_CONFIRMED', ?, ?, ?, ?)""",
            (f"pm-snapshot-{suffix}", f"pm-meta-{suffix}", source_name,
             _STATE_SOURCE_AS_OF, _STATE_INGESTED, _STATE_CUTOFF, grade),
        )
    conn.commit()
    conn.close()
    _insert_state_ai_explanations(db_path, _PM_BLOCKED_DRIVERS, _PM_BLOCKED_RISKS)
    monkeypatch.setenv("KSF_LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("KSF_READ_TOKEN", AUTH_VALUE)
    monkeypatch.delenv("KSF_READ_USERNAME", raising=False)
    monkeypatch.delenv("KSF_READ_PASSWORD", raising=False)
    client = TestClient(app)
    detail = client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    quality = detail.json()["data_quality"]
    assert quality.get("has_restricted") is True
    assert quality["state"] == "restricted"
    assert quality["run_status"] == "UNAVAILABLE"
    for path in _KSF_ENDPOINTS:
        response = client.get(path, headers=_auth())
        assert response.status_code == 200
        _assert_no_delimited_blocked_token(response.text)
        assert "4321.75" not in response.text
        if path.startswith("/ksf/cards"):
            # 대조군 소스명은 과차단되지 않는다 (source_statuses 는 JSON 에만 실린다).
            assert "unblocked_vendor_feed" in response.text
