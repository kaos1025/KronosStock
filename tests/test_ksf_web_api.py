from __future__ import annotations

import base64
import json
import os
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
            source_as_of, ingested_at_kst, available_data_cutoff, missing_reason)
           VALUES (?, ?, ?, 'regression', ?, 'v1', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (feature_id, run_id, symbol, feature_name, status, value, value_text,
         value_json, source_as_of, ingested_at, cutoff, missing_reason),
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


_POLICY_FORBIDDEN = (
    "display_policy", "do_not_display_or_score", "source_url", "terms_url",
    "vendor.example.com", "licensed-feed", "restricted-metadata", "0.91",
    "blocked.example", "4271.5", "restricted-directional-call",
)


def test_license_blocked_feature_value_is_suppressed_in_api(policy_client: TestClient):
    detail = policy_client.get("/ksf/cards/005930", headers=_auth())
    assert detail.status_code == 200
    card = detail.json()
    hbm = card["memory_indicators"]["hbm"]
    assert hbm == {
        "value": None,
        "status": "LICENSE_BLOCKED",
        "source_as_of": "2026-08-01T07:00:00+00:00",
    }
    nand = card["memory_indicators"]["nand"]
    assert nand["value"] is None
    # 안전한 license_blocked 표시는 유지된다.
    assert card["memory_indicators"]["license_blocked"] is True
    for needle in _POLICY_FORBIDDEN:
        assert needle not in detail.text, needle
    listing = policy_client.get("/ksf/cards", headers=_auth())
    assert listing.status_code == 200
    for needle in _POLICY_FORBIDDEN:
        assert needle not in listing.text, needle


def test_license_blocked_feature_value_is_suppressed_in_html(policy_client: TestClient):
    page = policy_client.get("/ksf/005930", headers=_auth())
    assert page.status_code == 200
    for needle in _POLICY_FORBIDDEN:
        assert needle not in page.text, needle
    # dict 가 str() 로 렌더링돼 새는 경우가 없어야 한다.
    assert "{'" not in page.text and "&#x27;value&#x27;" not in page.text
    assert "LICENSE_BLOCKED" in page.text  # 안전한 상태 표시는 유지


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
    assert web_api._feature_value({"hbm_supply_tightness": row}, ("hbm_supply_tightness",)) == {
        "value": None,
        "status": "LICENSE_BLOCKED",
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
