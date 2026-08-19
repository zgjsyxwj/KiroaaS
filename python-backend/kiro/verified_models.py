# -*- coding: utf-8 -*-

"""Account-scoped evidence for models accepted by the Kiro runtime."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, MutableMapping, Optional


@dataclass(frozen=True)
class VerifiedModelRecord:
    """A timestamped model verification owned by one Kiro account.

    Attributes:
        canonical_model_id: Canonical External Model ID.
        kiro_model_id: Kiro Model ID used by the successful request.
        verified_at: Unix timestamp of the successful request.
    """

    canonical_model_id: str
    kiro_model_id: str
    verified_at: float

    def as_model_record(self) -> Dict[str, str]:
        """Return the record in the shape consumed by Model Catalog building.

        Returns:
            A model record containing only routing metadata required for
            catalog construction.
        """
        return {
            "modelId": self.canonical_model_id,
            "kiroModelId": self.kiro_model_id,
        }


def is_verified_model_current(
    record: VerifiedModelRecord,
    ttl_seconds: float,
    *,
    now: Optional[float] = None,
) -> bool:
    """Return whether verification evidence is still within its TTL.

    Args:
        record: Account-owned verification evidence.
        ttl_seconds: Maximum age of evidence in seconds.
        now: Optional current Unix timestamp for deterministic callers/tests.

    Returns:
        ``True`` when the evidence has not exceeded its TTL.
    """
    current_time = time.time() if now is None else now
    return current_time - record.verified_at <= ttl_seconds


def get_current_verified_models(
    records: MutableMapping[str, VerifiedModelRecord],
    ttl_seconds: float,
    *,
    now: Optional[float] = None,
) -> List[VerifiedModelRecord]:
    """Return current evidence and remove expired records in place.

    Args:
        records: Mutable account-owned evidence keyed by canonical model ID.
        ttl_seconds: Maximum age of evidence in seconds.
        now: Optional current Unix timestamp for deterministic callers/tests.

    Returns:
        Current verification records in insertion order.
    """
    current_time = time.time() if now is None else now
    expired_model_ids = [
        model_id
        for model_id, record in records.items()
        if not is_verified_model_current(record, ttl_seconds, now=current_time)
    ]
    for model_id in expired_model_ids:
        del records[model_id]
    return list(records.values())
