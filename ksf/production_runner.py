"""Production one-shot orchestrator for the K-Semiconductor Flow Desk.

The orchestration surface is deliberately limited to read-only market-data
collectors and local SQLite ledger writes. Optional AI and settlement modules
remain explicit, inactive boundaries.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from inference.k_semiconductor_investor_flow_collector import (
    InvestorFlowCollector,
    InvestorFlowRepository,
    KisInvestorFlowClient,
)
from inference.k_semiconductor_domestic_price_collector import (
    collect_and_store as collect_domestic_price,
    latest_completed_xkrx_session,
)
from ksf.feature_engine import FeatureEngine
from ksf.global_collector import collect_global_inputs, stable_json


KST = timezone(timedelta(hours=9))
SUPPORTED_SYMBOLS = ("005930", "000660")
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
# 순서 보장: 001 이 fresh 스키마를 만들고, 002 가 기존 DB 에 남은 같은 이름의 legacy
# UPDATE guard trigger 를 DROP 후 현재 strict 정의로 교체한다.
MIGRATIONS = (
    MIGRATIONS_DIR / "001_k_semiconductor_flow_desk_permanent_ledgers.sql",
    MIGRATIONS_DIR / "002_k_semiconductor_flow_desk_update_guard_upgrade.sql",
)
REQUIRED_SCHEMA_VERSIONS = (1, 2)
REQUIRED_TABLES = frozenset(
    {
        "ksf_schema_versions",
        "ksf_symbols",
        "ksf_runs",
        "ksf_collected_source_metadata",
        "ksf_source_snapshots",
        "ksf_normalized_features",
        "ksf_decisions",
    }
)


class StageFailure(RuntimeError):
    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


DomesticCallable = Callable[..., Any]
DomesticPriceCallable = Callable[..., Any]
GlobalCallable = Callable[..., dict[str, Any]]


def _default_domestic(conn: sqlite3.Connection, *, symbols: Iterable[str], as_of_kst: str) -> Any:
    # 정확한 KST cutoff datetime 을 그대로 넘겨 최신 '완료' XKRX 세션 검증(15:30 마감 기준)을 받는다.
    cutoff = datetime.fromisoformat(as_of_kst).astimezone(KST)
    collector = InvestorFlowCollector(client=KisInvestorFlowClient(), now=lambda: cutoff, stale_after_days=0)
    records = collector.collect_symbols(symbols)
    return InvestorFlowRepository(conn).store_records(records, as_of_kst=as_of_kst)


def _default_domestic_price(
    conn: sqlite3.Connection,
    *,
    symbols: Iterable[str],
    as_of_kst: str,
    available_data_cutoff: str,
) -> Any:
    return collect_domestic_price(
        conn=conn,
        symbols=symbols,
        as_of_kst=as_of_kst,
        available_data_cutoff=available_data_cutoff,
    )


@dataclass(frozen=True)
class RunnerDependencies:
    domestic: DomesticCallable = _default_domestic
    domestic_price: DomesticPriceCallable = _default_domestic_price
    global_collect: GlobalCallable = collect_global_inputs
    now: Callable[[], datetime] = lambda: datetime.now(KST)
    migration_paths: tuple[Path, ...] = MIGRATIONS
    # 전역 trading_date 는 KST 달력 날짜가 아니라 최신 '완료' XKRX 세션이어야 한다(fail-closed).
    trading_session: Callable[[datetime], date] = latest_completed_xkrx_session


def _kst_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(KST).isoformat(timespec="seconds")


def _open_and_migrate(path: Path, migration_paths: Iterable[Path]) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    conn = sqlite3.connect(path)
    path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for migration_path in migration_paths:
            conn.executescript(migration_path.read_text(encoding="utf-8"))
        conn.execute("PRAGMA foreign_keys = ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("foreign key enforcement unavailable")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED_TABLES.issubset(tables):
            raise RuntimeError("required ledger schema is incomplete")
        placeholders = ",".join("?" for _ in REQUIRED_SCHEMA_VERSIONS)
        version_rows = conn.execute(
            f"SELECT version, COUNT(*) FROM ksf_schema_versions WHERE version IN ({placeholders})"
            " GROUP BY version",
            REQUIRED_SCHEMA_VERSIONS,
        ).fetchall()
        if sorted(tuple(row) for row in version_rows) != [(version, 1) for version in REQUIRED_SCHEMA_VERSIONS]:
            raise RuntimeError("ledger schema versions are invalid")
        return conn
    except Exception:
        conn.close()
        raise


def _domestic_counts(result: Any) -> dict[str, Any]:
    return {
        "status": "ok",
        "inserted_runs": int(result.inserted_runs),
        "inserted_snapshots": int(result.inserted_snapshots),
        "inserted_features": int(result.inserted_features),
    }


def _global_counts(results: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "symbols": len(results),
        "inserted_snapshots": sum(int(item.snapshots_inserted) for item in results.values()),
        "inserted_features": sum(int(item.features_inserted) for item in results.values()),
        "missing_sources": sum(
            status in {"MISSING", "STALE", "BLOCKED_REVIEW"}
            for item in results.values()
            for status in item.statuses.values()
        ),
    }


def run_once(db_path: str | Path, *, dependencies: RunnerDependencies | None = None) -> dict[str, Any]:
    deps = dependencies or RunnerDependencies()
    path = Path(db_path)
    try:
        conn = _open_and_migrate(path, deps.migration_paths)
    except Exception as exc:
        raise StageFailure("migration") from exc

    try:
        now = deps.now()
        as_of_kst = _kst_timestamp(now)
        cutoff = as_of_kst
        try:
            trading_date = deps.trading_session(now).isoformat()
        except Exception as exc:
            raise StageFailure("trading_session") from exc
        try:
            domestic_result = deps.domestic(conn, symbols=SUPPORTED_SYMBOLS, as_of_kst=as_of_kst)
        except Exception as exc:
            raise StageFailure("domestic") from exc
        try:
            domestic_price_result = deps.domestic_price(
                conn,
                symbols=SUPPORTED_SYMBOLS,
                as_of_kst=as_of_kst,
                available_data_cutoff=cutoff,
            )
        except Exception as exc:
            raise StageFailure("domestic_price") from exc
        try:
            global_results = deps.global_collect(
                conn,
                symbols=SUPPORTED_SYMBOLS,
                trading_date=trading_date,
                as_of_kst=as_of_kst,
                available_data_cutoff=cutoff,
            )
        except Exception as exc:
            raise StageFailure("global") from exc

        outputs = FeatureEngine(conn).build_many(
            SUPPORTED_SYMBOLS, available_data_cutoff=cutoff, as_of_kst=as_of_kst
        )
        if any(
            outputs[symbol].missing_required or outputs[symbol].stale_required
            for symbol in SUPPORTED_SYMBOLS
        ):
            raise StageFailure("feature_gate")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_key_errors:
            raise StageFailure("integrity")
        return {
            "status": "ok",
            "as_of_kst": as_of_kst,
            "available_data_cutoff": cutoff,
            "stages": {
                "migration": {"status": "ok", "schema_versions": list(REQUIRED_SCHEMA_VERSIONS)},
                "domestic": _domestic_counts(domestic_result),
                "domestic_price": _domestic_counts(domestic_price_result),
                "global": _global_counts(global_results),
                "feature_engine": {"status": "ok", "symbols": len(outputs)},
                "ai_explanation": {"status": "not_activated"},
                "performance_settlement": {"status": "not_activated"},
            },
            "symbols": [
                {
                    "symbol": symbol,
                    "state": outputs[symbol].state,
                    "missing_required": list(outputs[symbol].missing_required),
                    "missing_optional": list(outputs[symbol].missing_optional),
                    "stale_required": list(outputs[symbol].stale_required),
                    "license_blocked": list(outputs[symbol].license_blocked),
                    "output_id": outputs[symbol].output_id,
                }
                for symbol in SUPPORTED_SYMBOLS
            ],
            "db_integrity": {"integrity_check": "ok", "foreign_key_check": "ok"},
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None, *, dependencies: RunnerDependencies | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the K-Semiconductor Flow Desk once")
    parser.add_argument("--db", help="explicit SQLite ledger path (or set KSF_LEDGER_DB_PATH)")
    args = parser.parse_args(argv)
    db_path = args.db or os.getenv("KSF_LEDGER_DB_PATH")
    if not db_path:
        print(stable_json({"status": "failed", "failed_stage": "configuration"}))
        return 2
    try:
        summary = run_once(db_path, dependencies=dependencies)
    except StageFailure as exc:
        print(stable_json({"status": "failed", "failed_stage": exc.stage}))
        return 1
    except Exception:
        print(stable_json({"status": "failed", "failed_stage": "runner"}))
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
