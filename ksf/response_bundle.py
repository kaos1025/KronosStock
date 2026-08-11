"""Immutable, lineage-bound boundary for offline model responses."""
from __future__ import annotations

import hashlib
import json
from typing import Mapping

from paper_trading.ledger import LedgerError


SCHEMA_VERSION = "ksf-response-bundle-v1"
SYMBOLS = frozenset({"005930", "000660"})


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _valid_sha(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def build_response_bundle(*, session_id: str, cycle_at: str, source_artifact_sha256: str,
                          lineage: Mapping[str, Mapping[str, str]], responses: Mapping[str, object],
                          model_provider: str, model_name: str) -> dict[str, object]:
    if set(lineage) != SYMBOLS or set(responses) != SYMBOLS or not _valid_sha(source_artifact_sha256):
        raise LedgerError("response bundle inputs are incomplete")
    symbols = {}
    for symbol in sorted(SYMBOLS):
        item = lineage[symbol]
        if set(item) != {"ksf_run_id", "ksf_decision_id", "feature_snapshot_sha256"} \
                or not _valid_sha(item["feature_snapshot_sha256"]) or type(responses[symbol]) is not dict:
            raise LedgerError("response bundle lineage is malformed")
        symbols[symbol] = {**item, "response": responses[symbol]}
    body = {"schema_version": SCHEMA_VERSION, "session_id": session_id, "cycle_at": cycle_at,
            "source_artifact_sha256": source_artifact_sha256, "model_provider": model_provider,
            "model_name": model_name, "symbols": symbols}
    return {**body, "bundle_sha256": _hash(body)}


def validate_response_bundle(raw: bytes, *, session_id: str, cycle_at: str,
                             source_artifact_sha256: str, lineage: Mapping[str, Mapping[str, str]],
                             model_provider: str, model_name: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
        if type(value) is not dict or set(value) != {"schema_version", "session_id", "cycle_at",
                "source_artifact_sha256", "model_provider", "model_name", "symbols", "bundle_sha256"}:
            raise ValueError
        digest = value.pop("bundle_sha256")
        if not _valid_sha(digest) or _hash(value) != digest:
            raise ValueError
        if (value["schema_version"] != SCHEMA_VERSION or value["session_id"] != session_id
                or value["cycle_at"] != cycle_at or value["source_artifact_sha256"] != source_artifact_sha256
                or value["model_provider"] != model_provider or value["model_name"] != model_name
                or type(value["symbols"]) is not dict or set(value["symbols"]) != SYMBOLS):
            raise ValueError
        responses = {}
        for symbol in sorted(SYMBOLS):
            item = value["symbols"][symbol]
            expected = lineage[symbol]
            if type(item) is not dict or set(item) != {*expected, "response"} \
                    or any(item[key] != expected[key] for key in expected) or type(item["response"]) is not dict:
                raise ValueError
            responses[symbol] = item["response"]
        return responses
    except (KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise LedgerError("response bundle schema, hash, or provenance mismatch") from exc


__all__ = ["SCHEMA_VERSION", "build_response_bundle", "validate_response_bundle"]
