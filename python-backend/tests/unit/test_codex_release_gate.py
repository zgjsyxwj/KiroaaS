# -*- coding: utf-8 -*-

"""Tests for the explicit Codex release-gate decision boundary."""

import pytest

from kiro.codex_release_gate import REQUIRED_GATE_NAMES, evaluate_release_gate


def test_release_gate_stays_blocked_when_a_client_gate_is_not_verified() -> None:
    """Kiro API evidence cannot turn an unrun CLI/App gate into a pass."""
    decision = evaluate_release_gate(
        [
            {
                "name": "kiro_auth_model_discovery",
                "status": "passed",
                "client": "httpx",
                "version": "local-test",
                "request_phase": "POST /v1/responses",
                "protocol_difference": None,
            },
            {
                "name": "codex_cli_single_client_tool_loop",
                "status": "not_verified",
                "client": "codex_cli",
                "version": "0.147.0",
                "request_phase": "not_run",
                "protocol_difference": "Current CLI was not executed per release instructions",
            },
        ]
    )

    assert decision.release_ready is False
    assert "codex_cli_single_client_tool_loop" in decision.blocking_gates
    assert "codex_app_text" in decision.missing_gates


def test_release_gate_requires_protocol_difference_for_unverified_or_failed_gate() -> None:
    """A gate record must explain where client compatibility evidence is missing."""
    decision = evaluate_release_gate(
        [
            {
                "name": "codex_app_text",
                "status": "failed",
                "client": "codex_app",
                "version": "26.814.41407",
                "request_phase": "streaming response",
                "protocol_difference": "response.in_progress was required by the client but was absent",
            }
        ]
    )

    assert decision.release_ready is False
    assert "codex_app_text" in decision.blocking_gates
    assert decision.records[0]["protocol_difference"].startswith("response.in_progress")


def test_release_gate_passes_only_when_every_record_is_passed() -> None:
    """The release declaration requires the complete gate set."""
    records = [
        {
            "name": name,
            "status": "passed",
            "client": client,
            "version": version,
            "request_phase": "completed",
            "protocol_difference": None,
        }
        for name, client, version in (
            (name, "release-test", "fixture") for name in sorted(REQUIRED_GATE_NAMES)
        )
    ]

    decision = evaluate_release_gate(records)

    assert decision.release_ready is True
    assert decision.blocking_gates == []
    assert decision.missing_gates == []


def test_release_gate_rejects_empty_evidence_fields() -> None:
    """A placeholder client, version, or phase cannot satisfy the evidence schema."""
    with pytest.raises(ValueError, match="non-empty client"):
        evaluate_release_gate(
            [
                {
                    "name": "kiro_auth_model_discovery",
                    "status": "passed",
                    "client": "",
                    "version": "local-test",
                    "request_phase": "GET /v1/models",
                    "protocol_difference": None,
                }
            ]
        )


def test_release_gate_rejects_unknown_gate_names() -> None:
    """A caller cannot bypass the required gate set with an invented name."""
    with pytest.raises(ValueError, match="Unknown release gate name"):
        evaluate_release_gate(
            [
                {
                    "name": "invented_gate",
                    "status": "passed",
                    "client": "test",
                    "version": "fixture",
                    "request_phase": "completed",
                    "protocol_difference": None,
                }
            ]
        )
