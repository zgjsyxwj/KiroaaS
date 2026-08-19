# -*- coding: utf-8 -*-

"""Canonical Model Catalog construction for the Responses Provider boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Dict, Iterable, List, Literal, Mapping, Optional, Set

from kiro.model_resolver import normalize_model_name
from kiro.verified_models import VerifiedModelRecord


ModelSource = Literal["dynamic", "fallback"]

MODEL_SOURCE_DYNAMIC: ModelSource = "dynamic"
MODEL_SOURCE_FALLBACK: ModelSource = "fallback"
MODEL_OWNER = "Kiro"

GPT_56_CANONICAL_IDS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)

GPT_56_CREDIT_MULTIPLIERS: Dict[str, str] = {
    "gpt-5.6-sol": "2.4x",
    "gpt-5.6-terra": "1x",
    "gpt-5.6-luna": "0.1x",
}


@dataclass(frozen=True)
class ModelCatalogEntry:
    """A public canonical model and its separately tracked Kiro route ID.

    Attributes:
        external_model_id: Stable model ID exposed to API clients.
        kiro_model_id: Model ID sent to the Kiro runtime.
        owned_by: Ownership label exposed by the OpenAI-compatible API.
        description: Stable informational description for clients.
    """

    external_model_id: str
    kiro_model_id: str
    owned_by: str = MODEL_OWNER
    description: str = "Kiro model available for routing."


def build_model_catalog(
    discovered_models: Iterable[Mapping[str, Any]],
    curated_fallback_models: Iterable[Mapping[str, Any]],
    *,
    discovery_available: bool,
    aliases: Mapping[str, str],
    hidden_models: Mapping[str, str],
    hidden_from_list: Collection[str],
    verified_models: Iterable[VerifiedModelRecord] = (),
) -> List[ModelCatalogEntry]:
    """Build a unique, stable catalog from one authoritative model source.

    Dynamic discovery is authoritative when available. The fallback source is
    selected only when no account has usable dynamic discovery evidence. Alias
    names are accepted by the resolver but are never emitted here.

    Args:
        discovered_models: Model records returned by an account discovery API.
        curated_fallback_models: Maintained fallback model records.
        discovery_available: Whether discovered models are authoritative.
        aliases: Accepted input aliases that must remain hidden.
        hidden_models: Static external-to-Kiro mappings used by fallback mode.
        hidden_from_list: Canonical IDs intentionally excluded from discovery.
        verified_models: Current account-scoped runtime evidence to merge into
                         the catalog without changing the curated fallback.

    Returns:
        Canonical entries ordered with GPT 5.6 models first and all remaining
        IDs in stable lexical order.
    """
    if discovery_available:
        source_models = list(discovered_models)
    else:
        source_models = list(curated_fallback_models)
        source_models.extend(
            {
                "modelId": external_model_id,
                "kiroModelId": kiro_model_id,
            }
            for external_model_id, kiro_model_id in hidden_models.items()
        )

    source_models.extend(record.as_model_record() for record in verified_models)

    alias_names = {
        alias.strip()
        for alias in aliases
        if isinstance(alias, str) and alias.strip()
    }
    hidden_ids = _normalized_ids(hidden_from_list)
    candidates: Dict[str, ModelCatalogEntry] = {}

    for model_data in source_models:
        if not isinstance(model_data, Mapping):
            continue
        raw_external_id = model_data.get("modelId") or model_data.get("id")
        if isinstance(raw_external_id, str) and raw_external_id.strip() in alias_names:
            continue
        candidate = _catalog_entry_from_data(model_data)
        if candidate is None:
            continue
        if candidate.external_model_id in hidden_ids:
            continue

        previous = candidates.get(candidate.external_model_id)
        if previous is None or candidate.kiro_model_id < previous.kiro_model_id:
            candidates[candidate.external_model_id] = candidate

    return sorted(candidates.values(), key=_catalog_sort_key)


def _catalog_entry_from_data(
    model_data: Mapping[str, Any],
) -> Optional[ModelCatalogEntry]:
    """Convert one upstream model record into a canonical catalog entry.

    Args:
        model_data: Upstream record containing an external model ID and an
                    optional Kiro runtime ID.

    Returns:
        A canonical entry, or ``None`` for malformed records.
    """
    raw_external_id = model_data.get("modelId") or model_data.get("id")
    if not isinstance(raw_external_id, str) or not raw_external_id.strip():
        return None

    external_model_id = normalize_model_name(raw_external_id.strip())
    raw_kiro_id = (
        model_data.get("kiroModelId")
        or model_data.get("runtimeModelId")
        or model_data.get("_internal_id")
        or external_model_id
    )
    kiro_model_id = (
        raw_kiro_id.strip()
        if isinstance(raw_kiro_id, str) and raw_kiro_id.strip()
        else external_model_id
    )

    return ModelCatalogEntry(
        external_model_id=external_model_id,
        kiro_model_id=kiro_model_id,
        description=_description_for_model(external_model_id),
    )


def _normalized_ids(model_ids: Iterable[str]) -> Set[str]:
    """Normalize IDs used by filtering rules without retaining blank values.

    Args:
        model_ids: Model IDs to normalize.

    Returns:
        A set of non-empty normalized model IDs.
    """
    return {
        normalized
        for model_id in model_ids
        if isinstance(model_id, str)
        and (normalized := normalize_model_name(model_id.strip()))
    }


def _description_for_model(model_id: str) -> str:
    """Return a stable client-facing description for one canonical ID.

    Args:
        model_id: Canonical external model ID.

    Returns:
        Informational description for API clients.
    """
    multiplier = GPT_56_CREDIT_MULTIPLIERS.get(model_id)
    if multiplier is not None:
        return (
            f"Kiro {model_id} model. Credit multiplier: {multiplier}; "
            "informational only and does not affect routing or account selection."
        )
    return "Kiro model available for routing."


def _catalog_sort_key(entry: ModelCatalogEntry) -> tuple[int, str]:
    """Return the deterministic ordering key for a catalog entry.

    Args:
        entry: Canonical catalog entry to order.

    Returns:
        Priority and lexical key used for stable ordering.
    """
    try:
        priority = GPT_56_CANONICAL_IDS.index(entry.external_model_id)
    except ValueError:
        priority = len(GPT_56_CANONICAL_IDS)
    return priority, entry.external_model_id
