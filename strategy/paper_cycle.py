"""Disabled-by-default, event-sourced autonomous paper-cycle orchestrator.

All external inputs are injected.  This module has no network, broker, Redis,
provider, subprocess, or environment-file dependency.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ksf.feature_engine import SUPPORTED_SYMBOLS, SymbolFeatureOutput
from paper_trading.ledger import LedgerConflictError, LedgerError, PaperLedger
from strategy.paper_agent import (MODEL_POLICY_VERSION, PROMPT_TEMPLATE_VERSION,
    RESPONSE_SCHEMA_VERSION, propose)
from strategy.paper_broker import ExecutionPolicy, MarketObservation, execute_order
from strategy.paper_risk import RiskInput, RiskPolicy, review_and_persist


def stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(namespace: str, value: object) -> str:
    return namespace + "-" + hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()

KST = timedelta(hours=9)


class PaperCycleDisabled(ValueError):
    """Configuration did not explicitly enable the isolated paper runtime."""


@dataclass(frozen=True, slots=True)
class CycleLineage:
    symbol: str
    ksf_run_id: str
    ksf_decision_id: str
    feature_snapshot_sha256: str
    available_data_cutoff: str
    feature_snapshot: SymbolFeatureOutput


@dataclass(frozen=True, slots=True)
class CycleInput:
    session_id: str
    cycle_at: str
    lineages: tuple[CycleLineage, ...]
    observations: tuple[MarketObservation, ...]
    transport: Callable[[Mapping[str, object]], object]
    model_provider: str
    model_name: str
    response_bundle_sha256: str
    source_artifact_sha256: str


@dataclass(frozen=True, slots=True)
class CycleConfig:
    mode: str
    paper_db_path: Path
    account_id: str
    lock_path: Path
    source_name: str
    risk_policy: RiskPolicy
    execution_policy: ExecutionPolicy

    def __post_init__(self) -> None:
        if self.mode not in {"shadow", "fills"}:
            raise ValueError("mode must be explicit shadow or fills")
        for name in ("account_id", "source_name"):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise ValueError(f"{name} is required")
        if not self.paper_db_path.is_absolute() or not self.lock_path.is_absolute():
            raise ValueError("paper DB and lock paths must be explicit absolute paths")


@dataclass(frozen=True, slots=True)
class CycleResult:
    committed: bool
    replayed: bool
    skipped_lock: bool
    mode: str
    counts: dict[str, int]
    sanitized_digest: str
    alert_error: str | None


class _FileLock:
    def __init__(self, path: Path): self.path, self.fd = path, None
    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            os.close(self.fd); self.fd = None
            return False
    def __exit__(self, *_args) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN); os.close(self.fd)


def _parse_kst(value: str, name: str) -> datetime:
    try: parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc: raise LedgerError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != KST or not value.endswith("+09:00"):
        raise LedgerError(f"{name} must use explicit +09:00")
    return parsed


def _digest(value: object) -> str:
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


def _cycle_payload(config: CycleConfig, data: CycleInput) -> dict[str, object]:
    return {"account": config.account_id, "session": data.session_id, "cycle_at": data.cycle_at,
        "mode": config.mode, "source": config.source_name,
        "model_provider": data.model_provider, "model_name": data.model_name,
        "response_bundle_sha256": data.response_bundle_sha256,
        "source_artifact_sha256": data.source_artifact_sha256,
        "risk_policy": asdict(config.risk_policy),
        "execution_policy": asdict(config.execution_policy),
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "model_policy_version": MODEL_POLICY_VERSION,
        "observations": [{"symbol": x.symbol, "price": x.reference_price_krw,
            "observed_at": x.observed_at, "source": x.source_sha256}
            for x in sorted(data.observations, key=lambda x: (x.symbol, x.observed_at))],
        "lineage": [{"symbol": x.symbol, "run": x.ksf_run_id, "decision": x.ksf_decision_id,
            "snapshot": x.feature_snapshot_sha256, "cutoff": x.available_data_cutoff}
            for x in sorted(data.lineages, key=lambda x: x.symbol)]}


def _validate_inputs(config: CycleConfig, data: CycleInput) -> None:
    cycle_at = _parse_kst(data.cycle_at, "cycle_at")
    if cycle_at.date().isoformat() != data.session_id:
        raise LedgerError("session_id must equal cycle_at KST date")
    for name, digest in (("response bundle", data.response_bundle_sha256),
                         ("source artifact", data.source_artifact_sha256)):
        if (type(digest) is not str or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise LedgerError(f"{name} SHA must be lowercase hex")
    symbols = [item.symbol for item in data.lineages]
    if not symbols or len(symbols) != len(set(symbols)) or any(s not in SUPPORTED_SYMBOLS for s in symbols):
        raise LedgerError("lineage symbols must be unique and supported")
    for item in data.lineages:
        cutoff = _parse_kst(item.available_data_cutoff, "available_data_cutoff")
        if cutoff > cycle_at or type(item.feature_snapshot) is not SymbolFeatureOutput:
            raise LedgerError("future or malformed lineage")
        if item.feature_snapshot.symbol != item.symbol or item.feature_snapshot.available_data_cutoff != item.available_data_cutoff:
            raise LedgerError("cross-symbol or cutoff lineage mismatch")
        if _digest(json.loads(item.feature_snapshot.stable_json())) != item.feature_snapshot_sha256:
            raise LedgerError("feature snapshot hash mismatch")
    for obs in data.observations:
        if _parse_kst(obs.observed_at, "observation") > cycle_at:
            raise LedgerError("future observation")


def _authoritative_state(ledger: PaperLedger, account: str, at: datetime) -> None:
    def latest(table: str, allowed: set[str]) -> str | None:
        rows = ledger.conn.execute(f"SELECT seq,event_type,event_at FROM {table} WHERE account_id=? ORDER BY seq", (account,)).fetchall()
        chosen, previous = None, None
        for expected, row in enumerate(rows, 1):
            event_at = _parse_kst(row["event_at"], table)
            if type(row["seq"]) is not int or row["seq"] != expected or row["event_type"] not in allowed:
                raise LedgerError(f"corrupt {table}")
            if previous is not None and event_at < previous: raise LedgerError(f"corrupt {table} chronology")
            previous = event_at
            if event_at > at: raise LedgerError(f"future {table} state")
            chosen = row["event_type"]
        return chosen
    if latest("paper_account_events", {"CREATED", "ENABLED", "DISABLED"}) != "ENABLED":
        raise LedgerError("paper account is not enabled")
    if latest("paper_kill_switch_events", {"ENGAGED", "RELEASED"}) != "RELEASED":
        raise LedgerError("paper kill switch is engaged or missing")


def _close_price(lineage: CycleLineage) -> int:
    closes = [feature.value for group in lineage.feature_snapshot.groups.values()
        for feature in group.features if feature.name == "close"]
    if (len(closes) != 1 or isinstance(closes[0], bool)
            or not isinstance(closes[0], (int, float))
            or not float(closes[0]).is_integer() or closes[0] <= 0):
        raise LedgerError("canonical close mark is unavailable")
    return int(closes[0])


def _mark(ledger: PaperLedger, config: CycleConfig, data: CycleInput,
          cycle_id: str) -> dict[str, object]:
    positions, by_symbol = ledger.positions(config.account_id), {x.symbol: x for x in data.lineages}
    marks = {}
    for symbol, quantity in positions.items():
        lineage = by_symbol.get(symbol)
        if lineage is None: raise LedgerError("authoritative position lacks injected mark")
        marks[symbol] = {"quantity": quantity, "price_krw": _close_price(lineage),
            "feature_snapshot_sha256": lineage.feature_snapshot_sha256,
            "observed_at": lineage.available_data_cutoff}
    value = sum(x["quantity"] * x["price_krw"] for x in marks.values())
    cash = ledger.cash_balance(config.account_id)
    ledger.append_nav_snapshot(nav_snapshot_id=cycle_id + ":nav", account_id=config.account_id,
        session_date=data.session_id, cash_krw=cash, position_value_krw=value,
        nav_krw=cash + value, position_marks=marks, snapshot_at=data.cycle_at)
    row = ledger.conn.execute("SELECT * FROM paper_nav_snapshots WHERE nav_snapshot_id=?",
        (cycle_id + ":nav",)).fetchone()
    if row is None or row["account_id"] != config.account_id:
        raise LedgerError("authoritative cycle NAV is missing or account-mismatched")
    return dict(row)


def _counts(ledger: PaperLedger, account_id: str) -> dict[str, int]:
    queries = {
        "decisions": "SELECT count(*) FROM paper_agent_decisions WHERE account_id=?",
        "proposals": "SELECT count(*) FROM paper_order_proposals WHERE account_id=?",
        "reviews": "SELECT count(*) FROM paper_risk_reviews r JOIN paper_order_proposals p ON p.proposal_id=r.proposal_id WHERE p.account_id=?",
        "orders": "SELECT count(*) FROM paper_orders WHERE account_id=?",
        "fills": "SELECT count(*) FROM paper_fills WHERE account_id=?",
    }
    return {key: ledger.conn.execute(query, (account_id,)).fetchone()[0] for key, query in queries.items()}


def run_cycle(config: CycleConfig, data: CycleInput, *, alert: Callable[[str], None] | None = None,
              ledger_factory=PaperLedger, lock_factory=_FileLock,
              fault_hook: Callable[[str], None] | None = None) -> CycleResult:
    """Run one fully atomic cycle; alert delivery is deliberately post-commit."""
    hook = fault_hook or (lambda _stage: None)
    _validate_inputs(config, data)
    with lock_factory(config.lock_path) as acquired:
        if not acquired:
            return CycleResult(False, False, True, config.mode, {}, "paper-cycle status=skipped-lock", None)
        ledger = ledger_factory(config.paper_db_path)
        try:
            cycle_payload = _cycle_payload(config, data)
            cycle_id = stable_id("paper-cycle", cycle_payload)
            payload_sha256 = _digest(cycle_payload)
            with ledger.transaction_immediate():
                cycle_at = _parse_kst(data.cycle_at, "cycle_at")
                _authoritative_state(ledger, config.account_id, cycle_at)
                existing = ledger.conn.execute(
                    "SELECT cycle_id,payload_sha256,result_counts_json FROM paper_cycle_commits WHERE account_id=? AND session_id=?",
                    (config.account_id, data.session_id)).fetchone()
                if existing is not None and (existing["cycle_id"] != cycle_id or existing["payload_sha256"] != payload_sha256):
                    raise LedgerConflictError("session identity already exists with different payload")
                replayed = existing is not None
                before = _counts(ledger, config.account_id)

                if config.mode == "fills" and not replayed:
                    observations = {x.symbol: x for x in data.observations}
                    orders = ledger.conn.execute("SELECT * FROM paper_orders WHERE account_id=? ORDER BY order_id", (config.account_id,)).fetchall()
                    for order in orders:
                        if ledger.order_state(order["order_id"]) != "ACCEPTED" or order["symbol"] not in observations: continue
                        obs = observations[order["symbol"]]
                        execute_order(ledger, order_id=order["order_id"],
                            execution_id=stable_id("paper-fill", {"order": order["order_id"], "observation": obs.source_sha256,
                                "observed_at": obs.observed_at}), observation=obs,
                            policy=config.execution_policy, executed_at=obs.observed_at)
                hook("settle")
                cycle_nav = _mark(ledger, config, data, cycle_id) if not replayed else None
                hook("mark")
                _authoritative_state(ledger, config.account_id, cycle_at)

                if not replayed:
                    for lineage in sorted(data.lineages, key=lambda x: x.symbol):
                        positions = ledger.positions(config.account_id)
                        nav = cycle_nav
                        if nav is None or nav["nav_snapshot_id"] != cycle_id + ":nav" \
                                or nav["account_id"] != config.account_id:
                            raise LedgerError("authoritative cycle NAV is missing or account-mismatched")
                        quantity = positions.get(lineage.symbol, 0)
                        price = _close_price(lineage)
                        market_value = quantity * price
                        other_position_value = nav["position_value_krw"] - market_value
                        if type(other_position_value) is not int or other_position_value < 0:
                            raise LedgerError("authoritative other position value is invalid")
                        proposal = propose(lineage.feature_snapshot, {"symbol": lineage.symbol,
                            "cash_krw": ledger.cash_balance(config.account_id), "quantity": quantity,
                            "market_value_krw": market_value,
                            "other_position_value_krw": other_position_value,
                            "total_equity_krw": nav["nav_krw"],
                            "current_exposure_pct": (market_value * 100 / nav["nav_krw"]) if quantity else 0.0},
                            policy_limits={"max_target_exposure_pct": config.risk_policy.max_position_bps / 100},
                            model_provider=data.model_provider, model_name=data.model_name, transport=data.transport,
                            ksf_run_id=lineage.ksf_run_id, ksf_decision_id=lineage.ksf_decision_id,
                            feature_snapshot_sha256=lineage.feature_snapshot_sha256,
                            available_data_cutoff=lineage.available_data_cutoff)
                        hook("agent")
                        action = proposal["action"]
                        abstain_reason = proposal.get("abstain_reason")
                        reason_code = (
                            "DATA_MISSING" if abstain_reason == "DATA_MISSING_REQUIRED"
                            else "DATA_STALE" if abstain_reason in {"DATA_STALE_REQUIRED", "DATA_CUTOFF_INCONSISTENT"}
                            else "DATA_RESTRICTED" if abstain_reason == "DATA_RESTRICTED_REQUIRED"
                            else "MODEL_UNAVAILABLE" if abstain_reason in {"MODEL_TIMEOUT", "MODEL_PROVIDER_ERROR"}
                            else "MODEL_OUTPUT_INVALID" if abstain_reason == "MODEL_OUTPUT_INVALID"
                            else "FLOW_SIGNAL_NEGATIVE" if action in {"REDUCE", "EXIT"}
                            else "NO_EDGE" if action in {"HOLD", "ABSTAIN"} else "FLOW_SIGNAL_POSITIVE"
                        )
                        decision_id = stable_id("paper-decision", {"cycle": cycle_payload, "symbol": lineage.symbol, "proposal": proposal})
                        ledger.append_agent_decision(decision_id=decision_id, account_id=config.account_id,
                            symbol=lineage.symbol, ksf_run_id=lineage.ksf_run_id,
                            ksf_decision_id=lineage.ksf_decision_id, feature_snapshot_sha256=lineage.feature_snapshot_sha256,
                            available_data_cutoff=lineage.available_data_cutoff, action=action,
                            reason_code=reason_code,
                            rationale_sha256=_digest(proposal), created_at=data.cycle_at)
                        if action not in {"ENTER", "REDUCE", "EXIT"}: continue
                        side = "BUY" if action == "ENTER" else "SELL"
                        target = int(round(proposal["target_exposure_pct"] * 100))
                        proposal_id = stable_id("paper-proposal", {"decision": decision_id, "side": side, "target": target})
                        ledger.append_order_proposal(proposal_id=proposal_id, decision_id=decision_id, side=side,
                            target_exposure_bp=target, idempotency_key=proposal_id, model_name=data.model_name,
                            model_version="paper-agent-v1", proposed_at=data.cycle_at)
                        risk_input = RiskInput(account_id=config.account_id, ksf_run_id=lineage.ksf_run_id,
                            ksf_decision_id=lineage.ksf_decision_id, feature_snapshot_sha256=lineage.feature_snapshot_sha256,
                            session_id=data.session_id, symbol=lineage.symbol, action=action,
                            target_exposure_bps=target, price_krw=price, cash_krw=ledger.cash_balance(config.account_id),
                            holdings_quantity=quantity, aggregate_position_value_krw=nav["position_value_krw"],
                            nav_krw=nav["nav_krw"], high_water_nav_krw=nav["nav_krw"],
                            session_start_nav_krw=nav["nav_krw"], daily_order_count=0,
                            daily_fill_count=0,
                            trading_enabled=True, kill_switch_engaged=False, duplicate_state_snapshot=False,
                            decision_at=data.cycle_at, available_data_cutoff=lineage.available_data_cutoff,
                            feature_snapshot=lineage.feature_snapshot)
                        review_id = review_and_persist(ledger, proposal_id=proposal_id,
                            reviewed_at=data.cycle_at, request=risk_input, policy=config.risk_policy,
                            valuation_snapshots={x.symbol: x.feature_snapshot for x in data.lineages
                                if x.symbol != lineage.symbol and x.symbol in positions})
                        hook("risk")
                        review = ledger.conn.execute("SELECT verdict,approved_quantity FROM paper_risk_reviews WHERE review_id=?", (review_id,)).fetchone() if review_id else None
                        hook("order")
                        if config.mode == "fills" and review is not None and review["verdict"] in {"APPROVE", "RESIZE"}:
                            order_id = stable_id("paper-order", {"review": review_id})
                            ledger.append_order(order_id=order_id, review_id=review_id,
                                idempotency_key=order_id, created_at=data.cycle_at)
                    after = _counts(ledger, config.account_id)
                    counts = {name: after[name] - before[name] for name in after}
                    ledger.conn.execute(
                        "INSERT INTO paper_cycle_commits(cycle_id,account_id,session_id,mode,payload_sha256,result_counts_json,committed_at) VALUES(?,?,?,?,?,?,?)",
                        (cycle_id, config.account_id, data.session_id, config.mode, payload_sha256, stable_json(counts), data.cycle_at))
                else:
                    counts = json.loads(existing["result_counts_json"])
        finally:
            ledger.close()
    status = "replayed" if replayed else "committed"
    digest = "paper-cycle status=%s mode=%s decisions=%d proposals=%d reviews=%d orders=%d fills=%d" % (
        status, config.mode, counts["decisions"], counts["proposals"], counts["reviews"], counts["orders"], counts["fills"])
    alert_error = None
    if alert is not None and not replayed:
        try: alert(digest)
        except Exception: alert_error = "ALERT_FAILED"
    return CycleResult(not replayed, replayed, False, config.mode, counts, digest, alert_error)


def run_cycle_from_env(env: Mapping[str, str], *, ledger_factory=PaperLedger,
                       source_loader=None, lock_factory=_FileLock) -> CycleResult:
    """Validate every gate before opening a ledger, lock, or source."""
    if env.get("PAPER_AGENT_ENABLED") != "true":
        raise PaperCycleDisabled("PAPER_AGENT_ENABLED must be exactly true")
    if env.get("PAPER_AGENT_MODE") not in {"shadow", "fills"}:
        raise PaperCycleDisabled("PAPER_AGENT_MODE must be explicit shadow or fills")
    if env["PAPER_AGENT_MODE"] == "fills" and env.get("PAPER_AGENT_FILLS_ENABLED") != "true":
        raise PaperCycleDisabled("fills require PAPER_AGENT_FILLS_ENABLED exactly true")
    required = ("PAPER_AGENT_DB_PATH", "PAPER_AGENT_ACCOUNT_ID", "PAPER_AGENT_LOCK_PATH",
        "PAPER_AGENT_SOURCE", "PAPER_AGENT_KSF_DB_PATH", "PAPER_AGENT_HORIZON_DAYS",
        "PAPER_AGENT_SESSION_ID", "PAPER_AGENT_CYCLE_AT", "PAPER_AGENT_RISK_POLICY_JSON",
        "PAPER_AGENT_EXECUTION_POLICY_JSON", "PAPER_AGENT_MODEL_PROVIDER", "PAPER_AGENT_MODEL_NAME",
        "PAPER_AGENT_RESPONSE_BUNDLE_PATH")
    if any(not env.get(key) for key in required):
        raise PaperCycleDisabled("all offline paper runtime fields must be explicit")
    if env["PAPER_AGENT_SOURCE"] != "ksf-sqlite":
        raise PaperCycleDisabled("PAPER_AGENT_SOURCE must be exactly ksf-sqlite")
    paths = {name: Path(env[name]) for name in ("PAPER_AGENT_DB_PATH", "PAPER_AGENT_LOCK_PATH",
        "PAPER_AGENT_KSF_DB_PATH", "PAPER_AGENT_RESPONSE_BUNDLE_PATH")}
    if any(not path.is_absolute() for path in paths.values()):
        raise PaperCycleDisabled("all runtime paths must be absolute")
    try:
        horizon = int(env["PAPER_AGENT_HORIZON_DAYS"])
        if str(horizon) != env["PAPER_AGENT_HORIZON_DAYS"] or horizon not in {1, 5, 20}:
            raise ValueError
        risk = RiskPolicy.from_mapping(json.loads(env["PAPER_AGENT_RISK_POLICY_JSON"]))
        execution_raw = json.loads(env["PAPER_AGENT_EXECUTION_POLICY_JSON"])
        if type(execution_raw) is not dict or set(execution_raw) != {"fee_bps", "sell_tax_bps", "slippage_bps"}:
            raise ValueError
        execution = ExecutionPolicy(**execution_raw)
        _parse_kst(env["PAPER_AGENT_CYCLE_AT"], "cycle_at")
        datetime.strptime(env["PAPER_AGENT_SESSION_ID"], "%Y-%m-%d")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PaperCycleDisabled("offline paper runtime configuration is malformed") from exc
    for name in ("PAPER_AGENT_MODEL_PROVIDER", "PAPER_AGENT_MODEL_NAME", "PAPER_AGENT_ACCOUNT_ID"):
        if type(env[name]) is not str or not env[name].strip() or len(env[name]) > 256:
            raise PaperCycleDisabled("runtime identity fields are malformed")
    def read_regular(path: Path, limit: int, *, retain: bool) -> tuple[bytes | None, str]:
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise OSError
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
            try:
                chunks, size, digest = [], 0, hashlib.sha256()
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk: break
                    size += len(chunk)
                    if size > limit: raise OSError
                    digest.update(chunk)
                    if retain: chunks.append(chunk)
                return (b"".join(chunks) if retain else None), digest.hexdigest()
            finally: os.close(fd)
        except OSError:
            raise OSError

    try:
        raw, bundle_sha = read_regular(paths["PAPER_AGENT_RESPONSE_BUNDLE_PATH"],
            4 * 1024 * 1024, retain=True)
        _, source_sha = read_regular(paths["PAPER_AGENT_KSF_DB_PATH"],
            1024 * 1024 * 1024, retain=False)
        if raw is None:
            raise LedgerError("enabled response bundle bytes are unavailable")
    except OSError as exc:
        raise LedgerError("enabled runtime artifacts are unavailable or malformed") from exc
    loader = source_loader
    if loader is None:
        from strategy.paper_source import load_ksf_sqlite
        loader = load_ksf_sqlite
    lineages, observations = loader(paths["PAPER_AGENT_KSF_DB_PATH"],
        session_id=env["PAPER_AGENT_SESSION_ID"], horizon_days=horizon,
        cycle_at=env["PAPER_AGENT_CYCLE_AT"], source_sha256=source_sha)
    from ksf.response_bundle import validate_response_bundle
    lineage_map = {item.symbol: {"ksf_run_id": item.ksf_run_id,
        "ksf_decision_id": item.ksf_decision_id,
        "feature_snapshot_sha256": item.feature_snapshot_sha256} for item in lineages}
    responses = validate_response_bundle(raw, session_id=env["PAPER_AGENT_SESSION_ID"],
        cycle_at=env["PAPER_AGENT_CYCLE_AT"], source_artifact_sha256=source_sha,
        lineage=lineage_map, model_provider=env["PAPER_AGENT_MODEL_PROVIDER"],
        model_name=env["PAPER_AGENT_MODEL_NAME"])
    transport = lambda request: responses[request["symbol"]]
    config = CycleConfig(env["PAPER_AGENT_MODE"], paths["PAPER_AGENT_DB_PATH"],
        env["PAPER_AGENT_ACCOUNT_ID"], paths["PAPER_AGENT_LOCK_PATH"], "ksf-sqlite", risk, execution)
    data = CycleInput(env["PAPER_AGENT_SESSION_ID"], env["PAPER_AGENT_CYCLE_AT"],
        tuple(lineages), tuple(observations), transport, env["PAPER_AGENT_MODEL_PROVIDER"],
        env["PAPER_AGENT_MODEL_NAME"], bundle_sha, source_sha)
    return run_cycle(config, data, ledger_factory=ledger_factory, lock_factory=lock_factory)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated autonomous paper-cycle runner")
    parser.add_argument("--once", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        result = run_cycle_from_env(os.environ)
    except PaperCycleDisabled:
        print("paper-cycle status=disabled")
        return 0
    except Exception:
        print("paper-cycle status=failed", file=sys.stderr)
        return 1
    print(result.sanitized_digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
