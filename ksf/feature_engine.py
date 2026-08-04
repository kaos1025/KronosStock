"""Deterministic, symbol-independent K-Semiconductor feature engine.

This module only reads normalized SQLite ledger inputs.  It has no network,
broker, order, or external-write path.  Each requested symbol is evaluated in
isolation; global inputs are context for that symbol and never become pair or
relative-ranking features.
"""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ksf.global_collector import stable_id, stable_json


SUPPORTED_SYMBOLS = frozenset({"005930", "000660"})
CANONICAL_RUN_STATES = frozenset(
    {
        "READY",
        "PARTIAL_DATA",
        "STALE_DATA",
        "MISSING_REQUIRED_DATA",
        "SCORING_DONE",
        "AI_SUMMARY_DONE",
        "BLOCKED_REVIEW",
        "FAILED",
        "ARCHIVED",
    }
)

GROUP_ORDER = (
    "domestic_investor_flow",
    "price_volume",
    "global_memory_price",
    "ai_hbm_demand",
    "semiconductor_regime",
    "fx_market",
    "event",
)

# Caps are limits on the aggregate group vote, not per-feature allowances.
DEFAULT_GROUP_CAPS_BPS = MappingProxyType(
    {
        "domestic_investor_flow": 2500,
        "price_volume": 2500,
        "global_memory_price": 1500,
        "ai_hbm_demand": 1500,
        "semiconductor_regime": 1500,
        "fx_market": 500,
        "event": 500,
    }
)

REQUIRED_NAMES = frozenset(
    {"close", "volume", "foreigner_net_buy_quantity", "institution_total_net_buy_quantity"}
)
OPTIONAL_NAMES = frozenset(
    {
        "mu_one_day_return_bps",
        "nvda_one_day_return_bps",
        "soxx_one_day_return_bps",
        "tsm_twse_close",
        "usdkrw",
        "earnings_event_days",
    }
)


def validate_run_state(state: str) -> str:
    if state not in CANONICAL_RUN_STATES:
        raise ValueError(f"{state!r} is not a canonical run state")
    return state


def _parse_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone offset")
    return parsed


@dataclass(frozen=True)
class FeatureEngineConfig:
    supported_symbols: frozenset[str] = SUPPORTED_SYMBOLS
    group_caps_bps: Mapping[str, int] = field(default_factory=lambda: DEFAULT_GROUP_CAPS_BPS)

    def __post_init__(self) -> None:
        if set(self.group_caps_bps) != set(GROUP_ORDER):
            raise ValueError("group caps must cover exactly the canonical feature groups")
        if any(not isinstance(cap, int) or not 0 <= cap <= 10000 for cap in self.group_caps_bps.values()):
            raise ValueError("group caps must be integer basis points between 0 and 10000")


@dataclass(frozen=True)
class EngineFeature:
    name: str
    value: float | None
    status: str
    source_as_of: str
    source_feature_group: str
    unit: str | None
    missing_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "status": self.status,
            "source_as_of": self.source_as_of,
            "source_feature_group": self.source_feature_group,
            "unit": self.unit,
            "missing_reason": self.missing_reason,
        }


@dataclass(frozen=True)
class FeatureGroupOutput:
    name: str
    cap_bps: int
    contribution_bps: int
    features: tuple[EngineFeature, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cap_bps": self.cap_bps,
            "contribution_bps": self.contribution_bps,
            "features": [feature.to_dict() for feature in self.features],
        }


@dataclass(frozen=True)
class SymbolFeatureOutput:
    symbol: str
    state: str
    scoring_allowed: bool
    available_data_cutoff: str
    as_of_kst: str
    groups: Mapping[str, FeatureGroupOutput]
    missing_required: tuple[str, ...]
    missing_optional: tuple[str, ...]
    stale_required: tuple[str, ...]
    license_blocked: tuple[str, ...]
    output_id: str

    def to_dict(self) -> dict[str, Any]:
        # Deliberately contains no peer, pair, relative, or rank fields.
        return {
            "symbol": self.symbol,
            "state": self.state,
            "scoring_allowed": self.scoring_allowed,
            "available_data_cutoff": self.available_data_cutoff,
            "as_of_kst": self.as_of_kst,
            "groups": {name: self.groups[name].to_dict() for name in GROUP_ORDER},
            "missing_required": list(self.missing_required),
            "missing_optional": list(self.missing_optional),
            "stale_required": list(self.stale_required),
            "license_blocked": list(self.license_blocked),
            "output_id": self.output_id,
        }

    def stable_json(self) -> str:
        return stable_json(self.to_dict())


def _target_group(name: str, source_group: str) -> str | None:
    if source_group == "domestic_investor_flow" or name.endswith("_net_buy_quantity"):
        return "domestic_investor_flow"
    if source_group in {"domestic_price", "price_volume"} or name in {"close", "volume", "one_day_return_bps"}:
        return "price_volume"
    if name.startswith("mu_") or name.startswith(("dram_", "nand_")):
        return "global_memory_price"
    if name.startswith("nvda_") or name.startswith("hbm_"):
        return "ai_hbm_demand"
    if name.startswith(("soxx_", "sox_", "tsm_")):
        return "semiconductor_regime"
    if source_group in {"global_fx_context", "fx_market"} or name == "usdkrw":
        return "fx_market"
    if source_group in {"event", "event_group"} or name.endswith("_event_days"):
        return "event"
    return None


def _signal_bps(feature: EngineFeature) -> float | None:
    if feature.status != "READY" or feature.value is None or not math.isfinite(feature.value):
        return None
    name = feature.name
    if name.endswith("_one_day_return_bps") or name == "one_day_return_bps":
        return feature.value
    if name.endswith("_net_buy_quantity"):
        return max(-1000.0, min(1000.0, feature.value / 1000.0))
    # Absolute price, volume, FX and event-distance values have no direction
    # without a baseline, so they are carried as context but do not vote.
    return None


class FeatureEngine:
    def __init__(self, conn: sqlite3.Connection, config: FeatureEngineConfig | None = None) -> None:
        self.conn = conn
        self.config = config or FeatureEngineConfig()

    def build_many(
        self,
        symbols: Iterable[str],
        *,
        available_data_cutoff: str,
        as_of_kst: str,
    ) -> dict[str, SymbolFeatureOutput]:
        # Stable de-duplication preserves the explicit caller order. Each build
        # remains isolated and cannot depend on adjacency or iteration position.
        ordered_symbols = tuple(dict.fromkeys(symbols))
        return {
            symbol: self.build_symbol(
                symbol, available_data_cutoff=available_data_cutoff, as_of_kst=as_of_kst
            )
            for symbol in ordered_symbols
        }

    def build_symbol(
        self,
        symbol: str,
        *,
        available_data_cutoff: str,
        as_of_kst: str,
    ) -> SymbolFeatureOutput:
        if symbol not in self.config.supported_symbols:
            raise ValueError(f"unsupported symbol: {symbol}")
        if _parse_timestamp(available_data_cutoff) > _parse_timestamp(as_of_kst):
            raise ValueError("available_data_cutoff must not be after as_of_kst")

        selected = self._latest_features(symbol, available_data_cutoff)
        grouped: dict[str, list[EngineFeature]] = {name: [] for name in GROUP_ORDER}
        for row in selected.values():
            target = _target_group(row["feature_name"], row["feature_group"])
            if target is None:
                continue
            grouped[target].append(
                EngineFeature(
                    name=row["feature_name"],
                    value=row["value_num"],
                    status=row["feature_status"],
                    source_as_of=row["source_as_of"],
                    source_feature_group=row["feature_group"],
                    unit=row["unit"],
                    missing_reason=row["missing_reason"],
                )
            )

        names = set(selected)
        missing_required = tuple(sorted(REQUIRED_NAMES - names))
        stale_required = tuple(
            sorted(name for name in REQUIRED_NAMES & names if selected[name]["feature_status"] == "STALE")
        )
        unavailable_required = tuple(
            sorted(
                name
                for name in REQUIRED_NAMES & names
                if selected[name]["feature_status"] in {"MISSING_REQUIRED", "MISSING_OPTIONAL", "LICENSE_BLOCKED"}
                or selected[name]["value_num"] is None
            )
        )
        missing_required = tuple(sorted(set(missing_required) | set(unavailable_required)))
        missing_optional = tuple(
            sorted(
                name
                for name in OPTIONAL_NAMES
                if name not in names
                or selected[name]["feature_status"] in {"MISSING_OPTIONAL", "MISSING_REQUIRED"}
                or (selected[name]["value_num"] is None and selected[name]["feature_status"] != "LICENSE_BLOCKED")
            )
        )
        license_blocked = tuple(
            sorted(name for name, row in selected.items() if row["feature_status"] == "LICENSE_BLOCKED")
        )

        if missing_required:
            state = "MISSING_REQUIRED_DATA"
        elif stale_required:
            state = "STALE_DATA"
        elif missing_optional:
            state = "PARTIAL_DATA"
        else:
            state = "READY"
        validate_run_state(state)

        group_outputs: dict[str, FeatureGroupOutput] = {}
        for group_name in GROUP_ORDER:
            features = tuple(sorted(grouped[group_name], key=lambda item: item.name))
            votes = [vote for feature in features if (vote := _signal_bps(feature)) is not None]
            raw_vote = sum(votes) / len(votes) if votes else 0.0
            cap = self.config.group_caps_bps[group_name]
            contribution = int(round(max(-cap, min(cap, raw_vote))))
            group_outputs[group_name] = FeatureGroupOutput(group_name, cap, contribution, features)

        payload = {
            "symbol": symbol,
            "state": state,
            "scoring_allowed": state in {"READY", "PARTIAL_DATA"},
            "available_data_cutoff": available_data_cutoff,
            "as_of_kst": as_of_kst,
            "groups": {name: group_outputs[name].to_dict() for name in GROUP_ORDER},
            "missing_required": missing_required,
            "missing_optional": missing_optional,
            "stale_required": stale_required,
            "license_blocked": license_blocked,
        }
        output_id = stable_id("ksf_engine", payload)
        return SymbolFeatureOutput(
            symbol=symbol,
            state=state,
            scoring_allowed=state in {"READY", "PARTIAL_DATA"},
            available_data_cutoff=available_data_cutoff,
            as_of_kst=as_of_kst,
            groups=MappingProxyType(group_outputs),
            missing_required=missing_required,
            missing_optional=missing_optional,
            stale_required=stale_required,
            license_blocked=license_blocked,
            output_id=output_id,
        )

    def _latest_features(self, symbol: str, cutoff: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            """
            SELECT feature_id, feature_group, feature_name, feature_status,
                   value_num, unit, source_as_of, ingested_at_kst, missing_reason
            FROM ksf_normalized_features
            WHERE symbol = ?
              AND julianday(source_as_of) <= julianday(?)
              AND julianday(ingested_at_kst) <= julianday(?)
              AND julianday(available_data_cutoff) <= julianday(?)
            ORDER BY feature_name ASC, julianday(source_as_of) DESC,
                     julianday(ingested_at_kst) DESC, feature_id ASC
            """,
            (symbol, cutoff, cutoff, cutoff),
        ).fetchall()
        latest: dict[str, sqlite3.Row] = {}
        for row in rows:
            if row["feature_name"] not in latest:
                latest[row["feature_name"]] = row
        return latest


__all__ = [
    "CANONICAL_RUN_STATES",
    "FeatureEngine",
    "FeatureEngineConfig",
    "FeatureGroupOutput",
    "SymbolFeatureOutput",
    "validate_run_state",
]
