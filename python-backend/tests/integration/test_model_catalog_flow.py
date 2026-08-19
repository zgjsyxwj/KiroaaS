# -*- coding: utf-8 -*-

"""High-level, network-isolated tests for the canonical Model Catalog."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from kiro.account_manager import Account, AccountManager
from kiro.account_errors import ErrorType
from kiro.cache import ModelInfoCache
from kiro.config import FALLBACK_MODELS, HIDDEN_FROM_LIST, MODEL_ALIASES
from kiro.converters_openai import build_kiro_payload
from kiro.models_anthropic import AnthropicMessage, AnthropicMessagesRequest
from kiro.models_openai import ChatCompletionRequest, ChatMessage
from kiro.model_resolver import ModelResolver, get_model_id_for_kiro
from kiro import routes_anthropic, routes_openai
from kiro.routes_openai import get_models


def _request_for_account_manager(manager: AccountManager) -> Request:
    """Build a minimal request scope for invoking the models route."""
    app = SimpleNamespace(
        state=SimpleNamespace(account_manager=manager, account_system=True)
    )
    scope = {"type": "http", "method": "GET", "path": "/v1/models", "app": app}
    return Request(scope)


def _attach_model_resolver(
    account: Account,
    cache: ModelInfoCache,
    aliases: dict[str, str] | None,
) -> None:
    """Attach a resolver that shares the account's verification records.

    Args:
        account: Account that owns the resolver and verification records.
        cache: Model cache used by the resolver.
        aliases: Optional alias map for the resolver.

    Returns:
        None.
    """
    account.model_resolver = ModelResolver(
        cache=cache,
        aliases=MODEL_ALIASES if aliases is None else aliases,
        hidden_from_list=HIDDEN_FROM_LIST,
        verified_models=account.verified_models,
    )


def _manager_with_cache(
    cache: ModelInfoCache,
    *,
    aliases: dict[str, str] | None = None,
) -> AccountManager:
    """Create an account manager containing one prepared account.

    Args:
        cache: Model cache assigned to the account.
        aliases: Optional alias map for the account resolver.

    Returns:
        An account manager with one initialized test account.
    """
    account = Account(id="test-account")
    account.model_cache = cache
    _attach_model_resolver(account, cache, aliases)
    manager = AccountManager(credentials_file="", state_file="")
    manager._accounts = {account.id: account}
    return manager


def _manager_with_two_accounts(
    first_cache: ModelInfoCache,
    second_cache: ModelInfoCache,
    *,
    aliases: dict[str, str] | None = None,
) -> AccountManager:
    """Create a two-account manager with initialized model dependencies.

    Args:
        first_cache: Model cache assigned to the first account.
        second_cache: Model cache assigned to the second account.
        aliases: Optional alias map shared by both account resolvers.

    Returns:
        An account manager with two initialized test accounts.
    """
    manager = AccountManager(credentials_file="", state_file="")
    accounts = {}
    for account_id, cache in (("account-a", first_cache), ("account-b", second_cache)):
        account = Account(
            id=account_id,
            auth_manager=object(),
            model_cache=cache,
            models_cached_at=time.time(),
        )
        _attach_model_resolver(account, cache, aliases)
        accounts[account_id] = account
    manager._accounts = accounts
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


@pytest.mark.asyncio
async def test_successful_unknown_model_is_verified_only_for_accepting_account() -> None:
    """Runtime success creates isolated evidence and updates the catalog."""
    first_cache = ModelInfoCache()
    second_cache = ModelInfoCache()
    await first_cache.update([{"modelId": "known-a"}], source="dynamic")
    await second_cache.update([{"modelId": "known-b"}], source="dynamic")
    manager = _manager_with_two_accounts(first_cache, second_cache)

    await manager.report_success("account-a", "runtime-special-2026-08")

    first_record = manager._accounts["account-a"].verified_models["runtime-special-2026-08"]
    assert first_record.canonical_model_id == "runtime-special-2026-08"
    assert first_record.kiro_model_id == "runtime-special-2026-08"
    assert first_record.verified_at <= time.time()
    assert "runtime-special-2026-08" not in manager._accounts["account-b"].verified_models
    assert "runtime-special-2026-08" not in manager._model_to_accounts

    catalog_ids = [entry.external_model_id for entry in manager.get_model_catalog()]
    assert "runtime-special-2026-08" in catalog_ids
    assert (await manager.get_next_account("runtime-special-2026-08")).id == "account-a"
    assert (
        await manager.get_next_account(
            "runtime-special-2026-08",
            exclude_accounts={"account-a"},
        )
    ).id == "account-b"


@pytest.mark.asyncio
async def test_failed_call_alias_and_catalog_read_do_not_promote_verified_model() -> None:
    """Only a non-alias successful unknown call can create evidence."""
    cache = ModelInfoCache()
    await cache.update([{"modelId": "known-model"}], source="dynamic")
    manager = _manager_with_cache(
        cache,
        aliases={"friendly-model": "runtime-special-2026-08"},
    )
    account_id = "test-account"

    assert manager.get_model_catalog()
    await manager.report_failure(
        account_id,
        "runtime-special-2026-08",
        ErrorType.FATAL,
        400,
        "INVALID_MODEL_ID",
    )
    await manager.report_success(account_id, "friendly-model")

    assert manager._accounts[account_id].verified_models == {}
    catalog_ids = [entry.external_model_id for entry in manager.get_model_catalog()]
    assert "runtime-special-2026-08" not in catalog_ids
    assert "friendly-model" not in catalog_ids


@pytest.mark.asyncio
async def test_verified_model_renews_and_expires_from_catalog_and_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verification renewal extends TTL and expiry restores optimistic selection."""
    import kiro.account_manager as account_manager_module

    monkeypatch.setattr(account_manager_module, "ACCOUNT_CACHE_TTL", 10)
    current_time = 100.0
    monkeypatch.setattr(account_manager_module.time, "time", lambda: current_time)

    first_cache = ModelInfoCache()
    second_cache = ModelInfoCache()
    await first_cache.update([{"modelId": "known-a"}], source="dynamic")
    await second_cache.update([{"modelId": "known-b"}], source="dynamic")
    manager = _manager_with_two_accounts(first_cache, second_cache)

    await manager.report_success("account-a", "runtime-special-2026-08")
    first_timestamp = manager._accounts["account-a"].verified_models[
        "runtime-special-2026-08"
    ].verified_at

    current_time = 105.0
    await manager.report_success("account-a", "runtime-special-2026-08")
    renewed_timestamp = manager._accounts["account-a"].verified_models[
        "runtime-special-2026-08"
    ].verified_at
    assert renewed_timestamp > first_timestamp

    await first_cache.update(
        [{"modelId": "known-a"}, {"modelId": "runtime-special-2026-08"}],
        source="dynamic",
    )
    current_time = 106.0
    await manager.report_success("account-a", "runtime-special-2026-08")
    cache_renewed_timestamp = manager._accounts["account-a"].verified_models[
        "runtime-special-2026-08"
    ].verified_at
    assert cache_renewed_timestamp > renewed_timestamp

    await first_cache.update([{"modelId": "known-a"}], source="dynamic")
    current_time = 107.0
    catalog_ids = [entry.external_model_id for entry in manager.get_model_catalog()]
    assert "runtime-special-2026-08" in catalog_ids

    current_time = 117.0
    expired_catalog_ids = [
        entry.external_model_id for entry in manager.get_model_catalog()
    ]
    assert "runtime-special-2026-08" not in expired_catalog_ids
    manager._current_account_index = 1
    for account in manager._accounts.values():
        account.models_cached_at = current_time
    assert (await manager.get_next_account("runtime-special-2026-08")).id == "account-b"


@pytest.mark.parametrize(
    ("api_format", "stream"),
    [
        pytest.param("openai", False, id="openai-non-streaming"),
        pytest.param("openai", True, id="openai-streaming"),
        pytest.param("anthropic", False, id="anthropic-non-streaming"),
        pytest.param("anthropic", True, id="anthropic-streaming"),
    ],
)
@pytest.mark.asyncio
async def test_successful_responses_report_model_after_body_completion(
    monkeypatch: pytest.MonkeyPatch,
    api_format: str,
    stream: bool,
) -> None:
    """Responses report model success only after the response body completes.

    Args:
        monkeypatch: Pytest monkeypatch fixture for isolating the route adapters.
        api_format: Route format under test, either ``openai`` or ``anthropic``.
        stream: Whether to exercise the streaming response path.

    Returns:
        None.
    """
    auth_manager = SimpleNamespace(profile_arn="", api_host="https://kiro.test")
    account = Account(
        id="route-account",
        auth_manager=auth_manager,
        model_cache=ModelInfoCache(),
    )
    account_manager = SimpleNamespace(
        _accounts={account.id: account},
        get_next_account=AsyncMock(return_value=account),
        report_success=AsyncMock(),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            account_manager=account_manager,
            account_system=True,
            http_client=object(),
        )
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/messages" if api_format == "anthropic" else "/v1/chat/completions",
            "app": app,
        }
    )
    response = SimpleNamespace(status_code=200)
    http_client = SimpleNamespace(
        client=object(),
        request_with_retry=AsyncMock(return_value=response),
        close=AsyncMock(),
    )

    async def fake_stream(**kwargs):
        yield "data: complete\\n\\n"

    if api_format == "openai":
        route_module = routes_openai
        request_data = ChatCompletionRequest(
            model="runtime-special-2026-08",
            messages=[ChatMessage(role="user", content="hello")],
            stream=stream,
        )
        monkeypatch.setattr(route_module, "build_kiro_payload", lambda *args, **kwargs: {})
        if stream:
            monkeypatch.setattr(route_module, "stream_with_first_token_retry", fake_stream)
        else:
            monkeypatch.setattr(
                route_module,
                "collect_stream_response",
                AsyncMock(return_value={"id": "completion"}),
            )
        endpoint = route_module.chat_completions
    else:
        route_module = routes_anthropic
        request_data = AnthropicMessagesRequest(
            model="runtime-special-2026-08",
            messages=[AnthropicMessage(role="user", content="hello")],
            max_tokens=16,
            stream=stream,
        )
        monkeypatch.setattr(route_module, "anthropic_to_kiro", lambda *args, **kwargs: {})
        if stream:
            monkeypatch.setattr(
                route_module,
                "stream_with_first_token_retry_anthropic",
                fake_stream,
            )
        else:
            monkeypatch.setattr(
                route_module,
                "collect_anthropic_response",
                AsyncMock(return_value={"id": "message"}),
            )
        endpoint = route_module.messages

    monkeypatch.setattr(
        route_module,
        "KiroHttpClient",
        lambda auth_manager, shared_client=None: http_client,
    )

    result = await endpoint(request, request_data)
    if stream:
        account_manager.report_success.assert_not_awaited()
        async for _chunk in result.body_iterator:
            pass

    account_manager.report_success.assert_awaited_once_with(
        account.id,
        request_data.model,
    )


@pytest.mark.asyncio
async def test_verified_model_does_not_mutate_curated_fallback() -> None:
    """Runtime verification must remain separate from the curated fallback."""
    from copy import deepcopy

    cache = ModelInfoCache()
    await cache.update([{"modelId": "known-model"}], source="dynamic")
    manager = _manager_with_cache(cache)
    fallback_before = deepcopy(FALLBACK_MODELS)

    await manager.report_success("test-account", "runtime-special-2026-08")

    assert FALLBACK_MODELS == fallback_before
