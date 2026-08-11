"""Deterministic KSF scoring persistence over canonical FeatureEngine output."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Mapping

from ksf.feature_engine import GROUP_ORDER, SymbolFeatureOutput


SCORING_RULESET_VERSION = "ksf-deterministic-v1"
HORIZONS = (1, 5, 20)


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(namespace: str, value: object) -> str:
    return namespace + "_" + hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:24]


def _latest_rows(conn: sqlite3.Connection, symbol: str, cutoff: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        """SELECT * FROM ksf_normalized_features
           WHERE symbol=? AND julianday(source_as_of)<=julianday(?)
             AND julianday(ingested_at_kst)<=julianday(?)
             AND julianday(available_data_cutoff)<=julianday(?)
           ORDER BY feature_name, julianday(source_as_of) DESC,
                    julianday(ingested_at_kst) DESC, feature_id""",
        (symbol, cutoff, cutoff, cutoff),
    ).fetchall()
    selected: dict[str, sqlite3.Row] = {}
    for row in rows:
        selected.setdefault(row["feature_name"], row)
    return list(selected.values())


def persist_scoring(
    conn: sqlite3.Connection, *, trading_date: str, outputs: Mapping[str, SymbolFeatureOutput]
) -> int:
    """Materialize exact canonical inputs and append three decisions per symbol."""
    inserted = 0
    for symbol in sorted(outputs):
        output = outputs[symbol]
        if not output.scoring_allowed or output.missing_required or output.stale_required:
            raise ValueError("required feature gate is not satisfied")
        run_id = _id("ksf_scoring_run", {
            "symbol": symbol, "trading_date": trading_date,
            "cutoff": output.available_data_cutoff, "ruleset": SCORING_RULESET_VERSION,
        })
        conn.execute(
            """INSERT INTO ksf_runs
               (run_id,symbol,trading_date,run_status,as_of_kst,available_data_cutoff,
                scoring_ruleset_version,prompt_template_version,model_policy_version)
               VALUES(?,?,?,'SCORING_DONE',?,?,?,'none','none')
               ON CONFLICT(run_id) DO NOTHING""",
            (run_id, symbol, trading_date, output.as_of_kst,
             output.available_data_cutoff, SCORING_RULESET_VERSION),
        )
        for row in _latest_rows(conn, symbol, output.available_data_cutoff):
            feature_id = _id("ksf_scoring_feature", {"run": run_id, "source_feature": row["feature_id"]})
            conn.execute(
                """INSERT INTO ksf_normalized_features
                   (feature_id,run_id,symbol,source_snapshot_id,feature_group,feature_name,
                    feature_version,feature_status,value_num,value_text,value_json,unit,
                    source_as_of,ingested_at_kst,available_data_cutoff,contribution_cap_bps,missing_reason)
                   VALUES(?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(feature_id) DO NOTHING""",
                (feature_id, run_id, symbol, row["feature_group"], row["feature_name"],
                 row["feature_version"], row["feature_status"], row["value_num"],
                 row["value_text"], row["value_json"], row["unit"], row["source_as_of"],
                 row["ingested_at_kst"], output.available_data_cutoff,
                 row["contribution_cap_bps"], row["missing_reason"]),
            )
        snapshot_sha = hashlib.sha256(output.stable_json().encode("utf-8")).hexdigest()
        contributions = [
            {"feature_group": name, "contribution_bps": output.groups[name].contribution_bps,
             "cap_bps": output.groups[name].cap_bps}
            for name in GROUP_ORDER
        ]
        score = max(-100.0, min(100.0, sum(item["contribution_bps"] for item in contributions) / 100.0))
        label = "positive_watch" if score > 0 else "risk_watch" if score < 0 else "neutral_watch"
        opinion = "WATCH" if score > 0 else "CAUTION" if score < 0 else "NEUTRAL"
        rationale = {"method": "capped_group_contribution_sum", "optional_gaps": list(output.missing_optional),
                     "license_blocked": list(output.license_blocked)}
        for horizon in HORIZONS:
            decision_id = _id("ksf_decision", {"run": run_id, "horizon": horizon,
                                                "snapshot": snapshot_sha, "ruleset": SCORING_RULESET_VERSION})
            before = conn.total_changes
            conn.execute(
                """INSERT INTO ksf_decisions
                   (decision_id,run_id,symbol,horizon_days,as_of_kst,available_data_cutoff,
                    deterministic_score,score_label,user_opinion,scoring_ruleset_version,
                    feature_snapshot_sha256,feature_contributions_json,rationale_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(decision_id) DO NOTHING""",
                (decision_id, run_id, symbol, horizon, output.as_of_kst,
                 output.available_data_cutoff, score, label, opinion, SCORING_RULESET_VERSION,
                 snapshot_sha, _stable_json(contributions), _stable_json(rationale)),
            )
            inserted += conn.total_changes - before
    conn.commit()
    return inserted


__all__ = ["HORIZONS", "SCORING_RULESET_VERSION", "persist_scoring"]
