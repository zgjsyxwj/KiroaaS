# -*- coding: utf-8 -*-

"""High-level, network-isolated tests for the canonical Model Catalog."""

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from kiro.account_manager import Account, AccountManager
from kiro.cache import ModelInfoCache
from kiro.config import FALLBACK_MODELS, HIDDEN_FROM_LIST, MODEL_ALIASES
from kiro.converters_openai import build_kiro_payload
from kiro.models_openai import ChatCompletionRequest, ChatMessage
from kiro.model_resolver import ModelResolver, get_model_id_for_kiro
from kiro.routes_openai import get_models


def _request_for_account_manager(manager: AccountManager) -> Request:
    """Build a minimal request scope for invoking the models route."""
    app = SimpleNamespace(
        state=SimpleNamespace(account_manager=manager, account_system=True)
    )
    scope = {"type": "http", "method": "GET", "path": "/v1/models", "app": app}
    return Request(scope)


def _manager_with_cache(cache: ModelInfoCache) -> AccountManager:
    """Create an account manager containing one prepared account."""
    account = Account(id="test-account")
    account.model_cache = cache
    account.model_resolver = ModelResolver(
        cache=cache,
        aliases=MODEL_ALIASES,
        hidden_from_list=HIDDEN_FROM_LIST,
    )
    manager = AccountManager(credentials_file="", state_file="")
    manager._accounts = {account.id: account}
    return manager


@pytest.mark.asyncio
async def test_models_route_uses_dynamic_account_evidence_only() -> None:
    """Dynamic discovery must not be merged with static fallback models."""
    cache = ModelInfoCache()
    await cache.update(
        [
            {"modelId": "claude-sonnet-4-5"},
            {"modelId": "claude-sonnet-4.5"},
            {"modelId": "deepseek-3.2"},
            {"modelId": "gpt-5.6-luna"},
            {"modelId": "gpt-5.6-sol"},
            {"modelId": "gpt-5.6-terra"},
            {"modelId": "auto-kiro"},
            {"modelId": "dynamic-only"},
        ],
        source="dynamic",
    )
    manager = _manager_with_cache(cache)

    result = await get_models(_request_for_account_manager(manager))
    model_ids = [model.id for model in result.data]
    catalog = manager.get_model_catalog()

    assert model_ids[:3] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    assert model_ids == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "claude-sonnet-4.5",
        "deepseek-3.2",
        "dynamic-only",
    ]
    assert "dynamic-only" in model_ids
    assert "claude-sonnet-4.5" in model_ids
    assert "auto-kiro" not in model_ids
    assert "deepseek-3.2" in model_ids
    assert "glm-5" not in model_ids
    assert all(model.owned_by == "Kiro" for model in result.data)

    descriptions = {model.id: model.description or "" for model in result.data}
    assert "2.4x" in descriptions["gpt-5.6-sol"]
    assert "1x" in descriptions["gpt-5.6-terra"]
    assert "0.1x" in descriptions["gpt-5.6-luna"]
    assert "routing" in descriptions["gpt-5.6-sol"]
    sol_entry = next(entry for entry in catalog if entry.external_model_id == "gpt-5.6-sol")
    assert sol_entry.kiro_model_id == "gpt-5.6-sol"
    assert "claude" not in sol_entry.kiro_model_id

    routed_request = ChatCompletionRequest(
        model="gpt-5.6-sol",
        messages=[ChatMessage(role="user", content="hello")],
    )
    routed_payload = build_kiro_payload(
        routed_request,
        "conversation",
        "",
        model_id_override=sol_entry.kiro_model_id,
    )
    assert routed_payload["conversationState"]["currentMessage"]["userInputMessage"]["modelId"] == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_models_route_uses_curated_fallback_when_discovery_is_unavailable() -> None:
    """Fallback must expose the curated catalog when no dynamic evidence exists."""
    cache = ModelInfoCache()
    await cache.update(FALLBACK_MODELS, source="fallback")
    manager = _manager_with_cache(cache)

    result = await get_models(_request_for_account_manager(manager))
    model_ids = [model.id for model in result.data]

    assert model_ids[:3] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    assert "claude-sonnet-4.5" in model_ids
    assert "deepseek-3.2" in model_ids
    assert "glm-5" in model_ids
    assert "minimax-m2.1" in model_ids
    assert "qwen3-coder-next" in model_ids
    assert "auto-kiro" not in model_ids
    assert all(model.owned_by == "Kiro" for model in result.data)


@pytest.mark.asyncio
async def test_models_route_returns_a_stable_catalog_snapshot() -> None:
    """Repeated catalog reads must not create duplicate timestamp variants."""
    cache = ModelInfoCache()
    await cache.update([{"modelId": "gpt-5.6-sol"}], source="dynamic")
    manager = _manager_with_cache(cache)
    request = _request_for_account_manager(manager)

    first = await get_models(request)
    second = await get_models(request)

    assert first.model_dump() == second.model_dump()


@pytest.mark.asyncio
async def test_alias_remains_routable_but_is_not_a_catalog_entry() -> None:
    """An alias resolves to its Kiro ID without becoming a public duplicate."""
    cache = ModelInfoCache()
    await cache.update([{"modelId": "auto"}, {"modelId": "gpt-5.6-sol"}])
    resolver = ModelResolver(cache=cache, aliases={"auto-kiro": "auto"})

    resolution = resolver.resolve("auto-kiro")
    gpt_resolution = resolver.resolve("gpt-5.6-sol")

    assert resolution.internal_id == "auto"
    assert resolution.original_request == "auto-kiro"
    assert get_model_id_for_kiro("auto-kiro", {}, {"auto-kiro": "auto"}) == "auto"
    assert gpt_resolution.internal_id == "gpt-5.6-sol"
    assert gpt_resolution.original_request == "gpt-5.6-sol"

    separated_cache = ModelInfoCache()
    await separated_cache.update(
        [{"modelId": "external-model", "kiroModelId": "runtime-model"}],
        source="dynamic",
    )
    separated_resolution = ModelResolver(cache=separated_cache).resolve("external-model")
    assert separated_resolution.internal_id == "runtime-model"
