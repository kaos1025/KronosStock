"""Strict read-only KSF SQLite adapter for the offline paper runtime."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path

from ksf.feature_engine import FeatureEngine, SUPPORTED_SYMBOLS
from paper_trading.ledger import LedgerError
from strategy.paper_broker import MarketObservation


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def load_ksf_sqlite(path: Path, *, session_id: str, horizon_days: int,
                    cycle_at: str, source_sha256: str):
    """Load complete canonical snapshots without ever opening the source writable."""
    try:
        if not path.is_absolute() or not stat.S_ISREG(path.lstat().st_mode):
            raise OSError
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        try:
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk: break
                digest.update(chunk)
        finally:
            os.close(fd)
        if digest.hexdigest() != source_sha256:
            raise OSError
    except OSError as exc:
        raise LedgerError("KSF source is unavailable") from exc
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise LedgerError("KSF source is unavailable") from exc
    conn.row_factory = sqlite3.Row
    try:
        if not {"ksf_runs", "ksf_decisions", "ksf_normalized_features"} <= _tables(conn):
            raise LedgerError("KSF source schema is incomplete")
        lineages, observations = [], []
        from strategy.paper_cycle import CycleLineage
        for symbol in sorted(SUPPORTED_SYMBOLS):
            runs = conn.execute(
                """SELECT * FROM ksf_runs WHERE symbol=? AND trading_date=?
                   AND run_status IN ('SCORING_DONE','AI_SUMMARY_DONE')
                   AND archived_at_kst IS NULL AND julianday(as_of_kst)<=julianday(?)
                   AND julianday(available_data_cutoff)<=julianday(?)
                   ORDER BY julianday(as_of_kst) DESC, run_id DESC""",
                (symbol, session_id, cycle_at, cycle_at)).fetchall()
            if not runs:
                raise LedgerError("eligible exact-session KSF run is missing")
            run = runs[0]
            if len(runs) > 1 and runs[1]["as_of_kst"] == run["as_of_kst"]:
                raise LedgerError("duplicate latest KSF run")
            decisions = conn.execute(
                "SELECT * FROM ksf_decisions WHERE run_id=? AND symbol=? AND horizon_days=?",
                (run["run_id"], symbol, horizon_days)).fetchall()
            if len(decisions) != 1:
                raise LedgerError("exact-horizon KSF decision is missing or duplicate")
            decision = decisions[0]
            if (decision["available_data_cutoff"] != run["available_data_cutoff"]
                    or decision["as_of_kst"] != run["as_of_kst"]):
                raise LedgerError("KSF decision lineage mismatch")
            rows = conn.execute("SELECT * FROM ksf_normalized_features WHERE run_id=?",
                                (run["run_id"],)).fetchall()
            if not rows:
                raise LedgerError("KSF feature rows are missing")
            if (any(row["symbol"] != symbol for row in rows)
                    or len({row["feature_name"] for row in rows}) != len(rows)):
                raise LedgerError("duplicate or cross-symbol KSF feature rows")
            memory = sqlite3.connect(":memory:")
            memory.row_factory = sqlite3.Row
            memory.execute("""CREATE TABLE ksf_normalized_features (
                feature_id TEXT, symbol TEXT, feature_group TEXT, feature_name TEXT,
                value_num REAL, unit TEXT, source_as_of TEXT, feature_status TEXT,
                missing_reason TEXT, ingested_at_kst TEXT, available_data_cutoff TEXT)""")
            for row in rows:
                if row["available_data_cutoff"] != run["available_data_cutoff"]:
                    raise LedgerError("cross-cutoff KSF feature row")
                try:
                    cutoff = datetime.fromisoformat(run["available_data_cutoff"])
                    source_at = datetime.fromisoformat(row["source_as_of"])
                    ingested_at = datetime.fromisoformat(row["ingested_at_kst"])
                except (TypeError, ValueError) as exc:
                    raise LedgerError("KSF feature timestamp is malformed") from exc
                if (cutoff.utcoffset() is None or source_at.utcoffset() is None
                        or ingested_at.utcoffset() is None or source_at > cutoff or ingested_at > cutoff):
                    raise LedgerError("future KSF feature row")
                memory.execute("INSERT INTO ksf_normalized_features VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                    row["feature_id"], row["symbol"], row["feature_group"], row["feature_name"],
                    row["value_num"], row["unit"], row["source_as_of"], row["feature_status"],
                    row["missing_reason"], row["ingested_at_kst"], row["available_data_cutoff"]))
            snapshot = FeatureEngine(memory).build_symbol(symbol,
                available_data_cutoff=run["available_data_cutoff"], as_of_kst=run["as_of_kst"])
            memory.close()
            if not snapshot.scoring_allowed or snapshot.missing_required or snapshot.stale_required:
                raise LedgerError("KSF feature snapshot is incomplete or stale")
            digest = hashlib.sha256(snapshot.stable_json().encode("utf-8")).hexdigest()
            if digest != decision["feature_snapshot_sha256"]:
                raise LedgerError("KSF feature snapshot hash mismatch")
            closes = [f.value for g in snapshot.groups.values() for f in g.features if f.name == "close"]
            if len(closes) != 1 or not float(closes[0]).is_integer() or closes[0] <= 0:
                raise LedgerError("canonical close is unavailable")
            lineages.append(CycleLineage(symbol, run["run_id"], decision["decision_id"], digest,
                run["available_data_cutoff"], snapshot))
            observations.append(MarketObservation(symbol, int(closes[0]),
                run["available_data_cutoff"], source_sha256))
        return tuple(lineages), tuple(observations)
    except sqlite3.Error as exc:
        raise LedgerError("KSF source schema is invalid") from exc
    finally:
        conn.close()
