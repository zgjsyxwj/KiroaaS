# -*- coding: utf-8 -*-

"""Network-isolated provenance and parser checks for the Codex release gate."""

import base64
import json
import socket
from pathlib import Path
from typing import Any, Dict, List

from kiro.codex_release_gate import REQUIRED_GATE_NAMES, evaluate_release_gate
from kiro.parsers import AwsEventStreamParser


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "responses"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"


def _load_json(path: Path) -> Dict[str, Any]:
    """Load one checked-in JSON fixture."""
    return json.loads(path.read_text(encoding="utf-8"))


def test_responses_fixture_manifest_covers_each_fixture_with_provenance() -> None:
    """Every release-gate fixture declares an auditable source class."""
    manifest = _load_json(MANIFEST_PATH)
    entries = manifest["fixtures"]
    declared_paths = {entry["path"] for entry in entries}
    actual_paths = {
        path.relative_to(FIXTURE_ROOT).as_posix()
        for path in FIXTURE_ROOT.rglob("*.json")
        if path.name not in {"manifest.json", "release_gate_records.json"}
    }

    assert declared_paths == actual_paths
    assert len(entries) == len(declared_paths)
    assert {entry["kind"] for entry in entries} == {
        "official_contract",
        "sanitized_codex_capture",
        "adversarial_aws_stream",
    }
    for entry in entries:
        provenance = entry["provenance"]
        fixture = _load_json(FIXTURE_ROOT / entry["path"])
        fixture_provenance = fixture["provenance"]
        assert fixture_provenance["kind"] == entry["kind"]
        assert provenance["source"]
        assert "captured_at" in provenance
        assert provenance["recorded_at"]
        assert provenance["not_kiroproxy_capture"] is True
        for key, value in provenance.items():
            assert fixture_provenance.get(key) == value


def test_official_contract_is_distinguished_from_client_capture() -> None:
    """The official contract fixture is documentation evidence, not a capture."""
    fixture = _load_json(FIXTURE_ROOT / "official/minimal_responses_contract.json")

    assert fixture["provenance"]["kind"] == "official_contract"
    assert "developers.openai.com" in fixture["provenance"]["source"]
    assert fixture["provenance"]["source_type"] == "official_documentation"
    assert fixture["response"]["object"] == "response"
    assert fixture["stream"]["events"][0]["type"] == "response.created"
    assert fixture["stream"]["events"][-1]["type"] == "response.completed"


def test_sanitized_capture_records_client_versions_without_claiming_unrun_evidence() -> None:
    """Client capture slots are explicit when the release gate was not run."""
    capture_specs = [
        ("codex_cli_0.147.0_sanitized.json", "codex_cli", "0.147.0"),
        ("codex_app_26.814.41407_sanitized.json", "codex_app", "26.814.41407"),
    ]
    for filename, client, version in capture_specs:
        fixture = _load_json(FIXTURE_ROOT / "captures" / filename)
        serialized = json.dumps(fixture, ensure_ascii=False).lower()

        assert fixture["provenance"]["kind"] == "sanitized_codex_capture"
        assert fixture["provenance"]["client"] == client
        assert fixture["provenance"]["version"] == version
        assert fixture["provenance"]["sanitized"] is True
        assert fixture["provenance"]["status"] == "not_verified"
        assert fixture["capture"] is None
        assert "authorization" not in serialized
        assert "bearer " not in serialized
        assert "kiroproxy" not in fixture["provenance"]["source"].lower()


def test_release_gate_record_schema_blocks_unverified_client_evidence() -> None:
    """The checked-in gate report must be evaluated, not merely documented."""
    report = _load_json(FIXTURE_ROOT / "release_gate_records.json")
    assert set(report["required_gates"]) == REQUIRED_GATE_NAMES

    decision = evaluate_release_gate(report["records"])

    assert decision.release_ready is report["expected_release_ready"]
    assert set(decision.blocking_gates) == set(report["expected_blocking_gates"])
    assert decision.missing_gates == []

    capture_metadata = {
        "codex_cli": ("Codex CLI", "0.147.0", "codex_cli"),
        "codex_app": ("Codex App", "26.814.41407", "codex_app"),
    }
    for client_key, (client_name, version, gate_prefix) in capture_metadata.items():
        capture = _load_json(
            FIXTURE_ROOT / "captures" / f"{client_key}_{version}_sanitized.json"
        )["provenance"]
        matching_records = [
            record
            for record in report["records"]
            if record["name"].startswith(f"{gate_prefix}_")
        ]
        assert matching_records
        assert all(record["client"] == client_name for record in matching_records)
        assert all(record["version"] == capture["version"] for record in matching_records)
        assert all(record["status"] == capture["status"] for record in matching_records)


def test_fixture_parser_does_not_open_network(monkeypatch: Any) -> None:
    """The checked-in fixture path stays local even if socket use is attempted."""
    def fail_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("fixture tests must not open a network socket")

    monkeypatch.setattr(socket, "socket", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)

    manifest = _load_json(MANIFEST_PATH)
    for entry in manifest["fixtures"]:
        assert _load_json(FIXTURE_ROOT / entry["path"])["provenance"]["source"]


def test_adversarial_aws_stream_survives_transport_chunk_boundaries() -> None:
    """The strict parser consumes complete AWS frames, not HTTP chunk guesses."""
    fixture = _load_json(FIXTURE_ROOT / "adversarial/aws_cross_chunk_stream.json")
    wire = base64.b64decode(fixture["wire_base64"])
    chunks = _split_at_boundaries(wire, fixture["chunk_ends"])
    parser = AwsEventStreamParser(allow_legacy_json=False)
    parsed: List[Dict[str, Any]] = []

    for chunk in chunks:
        parsed.extend(parser.feed(chunk))
    parser.finalize()

    assert parsed == fixture["expected_events"]


def _split_at_boundaries(wire: bytes, ends: List[int]) -> List[bytes]:
    """Split one fixture wire image at its recorded HTTP transport boundaries."""
    assert ends == sorted(ends)
    assert ends[-1] == len(wire)
    start = 0
    chunks = []
    for end in ends:
        chunks.append(wire[start:end])
        start = end
    return chunks
