# -*- coding: utf-8 -*-

"""Deterministic release-gate decision logic for Responses client evidence."""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional


_ALLOWED_STATUSES = frozenset({"passed", "failed", "not_verified", "blocked"})
REQUIRED_GATE_NAMES = frozenset(
    {
        "kiro_auth_model_discovery",
        "kiro_non_stream_text",
        "kiro_stream_text",
        "kiro_unsupported_capability_error",
        "network_isolated_fixture_tests",
        "chat_completions_regression",
        "anthropic_regression",
        "frontend_tests_and_build",
        "codex_cli_auth_model_discovery",
        "codex_cli_non_stream_text",
        "codex_cli_stream_text",
        "codex_cli_single_client_tool_loop",
        "codex_cli_multi_tool_call",
        "codex_cli_cancel",
        "codex_cli_unsupported_capability_error",
        "codex_app_text",
        "codex_app_client_tool_loop",
    }
)
_REQUIRED_FIELDS = frozenset(
    {"name", "status", "client", "version", "request_phase", "protocol_difference"}
)


@dataclass(frozen=True)
class ReleaseGateDecision:
    """The immutable result of evaluating all release-gate records."""

    records: List[Dict[str, Any]]
    blocking_gates: List[str]
    missing_gates: List[str]
    release_ready: bool

    def as_dict(self) -> Dict[str, Any]:
        """Serialize the decision for a human-readable or machine report."""
        return {
            "records": self.records,
            "blocking_gates": self.blocking_gates,
            "missing_gates": self.missing_gates,
            "required_gates": sorted(REQUIRED_GATE_NAMES),
            "release_ready": self.release_ready,
        }


def evaluate_release_gate(
    records: Iterable[Mapping[str, Any]],
) -> ReleaseGateDecision:
    """Evaluate a conjunctive release gate without inferring missing evidence.

    Args:
        records: Gate records. Each record must identify the client/version,
            request phase, status, and a protocol difference for non-passes.

    Returns:
        A decision that is ready only when every required gate is present and
        passed. Missing evidence is a blocking condition, not an implicit pass.

    Raises:
        ValueError: If a record is incomplete, duplicated, or uses an unknown
            status. A failed or unverified record must carry an explanation.
    """
    normalized: List[Dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw_record in records:
        record = dict(raw_record)
        missing = _REQUIRED_FIELDS.difference(record)
        if missing:
            raise ValueError(
                "Release gate record is missing fields: " + ", ".join(sorted(missing))
            )
        name = record["name"]
        status = record["status"]
        if not isinstance(name, str) or not name:
            raise ValueError("Release gate name must be a non-empty string")
        record["name"] = name = name.strip()
        if not name:
            raise ValueError("Release gate name must be a non-empty string")
        if name in seen_names:
            raise ValueError(f"Duplicate release gate record: {name}")
        if name not in REQUIRED_GATE_NAMES:
            raise ValueError(f"Unknown release gate name: {name}")
        if not isinstance(status, str) or status not in _ALLOWED_STATUSES:
            raise ValueError(f"Unknown release gate status for {name}: {status}")
        for field in ("client", "version", "request_phase"):
            value = record[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Release gate {name} requires non-empty {field}")
        if status != "passed":
            difference: Optional[str] = record["protocol_difference"]
            if not isinstance(difference, str) or not difference.strip():
                raise ValueError(
                    f"Non-passing release gate {name} must record protocol_difference"
                )
        seen_names.add(name)
        normalized.append(record)

    missing_gates = sorted(REQUIRED_GATE_NAMES.difference(seen_names))
    blocking_gates = missing_gates + [
        record["name"] for record in normalized if record["status"] != "passed"
    ]
    return ReleaseGateDecision(
        records=normalized,
        blocking_gates=blocking_gates,
        missing_gates=missing_gates,
        release_ready=not missing_gates and not blocking_gates,
    )
