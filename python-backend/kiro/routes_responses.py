# -*- coding: utf-8 -*-

"""FastAPI routes for the stateless Responses provider."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from kiro.account_errors import ErrorType, classify_error
from kiro.config import FIRST_TOKEN_MAX_RETRIES, FIRST_TOKEN_TIMEOUT, PROFILE_ARN
from kiro.converters_responses import (
    ResponsesConversionError,
    ResponsesRequestIR,
    convert_responses_request,
)
from kiro.http_client import KiroHttpClient
from kiro.kiro_errors import enhance_kiro_error
from kiro.models_responses import ResponsesRequest
from kiro.routes_openai import verify_api_key
from kiro.responses_provider import (
    build_responses_kiro_payload,
    build_responses_object,
    generate_response_id,
)
from kiro.responses_streaming import (
    ResponsesStreamState,
    encode_response_sse,
)
from kiro.streaming_core import (
    FirstTokenTimeoutError,
    KiroEvent,
    collect_stream_to_result,
    parse_kiro_stream,
)

try:
    from kiro.debug_logger import debug_logger
except ImportError:
    debug_logger = None


router = APIRouter()


@dataclass
class _PreparedResponsesStream:
    """Upstream resources prepared before the first public Responses event."""

    account: Any
    http_client: KiroHttpClient
    upstream_response: httpx.Response
    parsed_stream: AsyncGenerator[KiroEvent, None]
    first_event: Optional[KiroEvent]
    state: ResponsesStreamState


async def _close_upstream_resources(
    http_client: KiroHttpClient,
    upstream_response: Optional[httpx.Response],
    parsed_stream: Optional[AsyncGenerator[KiroEvent, None]],
) -> None:
    """Close a parser, response, and request-scoped client in dependency order."""
    if parsed_stream is not None:
        try:
            await parsed_stream.aclose()
        except RuntimeError:
            logger.debug("Responses parser was already closed")
    if upstream_response is not None:
        try:
            await upstream_response.aclose()
        except httpx.HTTPError:
            logger.debug("Failed to close Responses upstream response")
    await http_client.close()


async def _select_account(
    request: Request,
    model: str,
    exclude_accounts: Optional[set[str]] = None,
) -> Any:
    """Select the account using the application's existing account policy.

    Args:
        request: FastAPI request containing application account state.
        model: External model ID requested by the client.
        exclude_accounts: Account IDs already attempted for this request.

    Returns:
        An initialized account selected by the existing routing policy.

    Raises:
        HTTPException: If no initialized account is available.
    """
    account_manager = request.app.state.account_manager
    if request.app.state.account_system:
        account = await account_manager.get_next_account(
            model,
            exclude_accounts=exclude_accounts,
        )
    else:
        account = account_manager.get_first_account()
    if account is None or account.auth_manager is None:
        raise HTTPException(status_code=503, detail="No initialized Kiro account is available")
    return account


async def _read_upstream_error(
    response: httpx.Response,
) -> tuple[str, Optional[str]]:
    """Read and sanitize one Kiro error response for the client.

    Args:
        response: Completed non-success response from Kiro.

    Returns:
        A safe user-facing error message and the optional Kiro reason code.
    """
    try:
        body = await response.aread()
    except httpx.HTTPError:
        return "Kiro returned an error without a readable body", None
    text = body.decode("utf-8", errors="replace")
    try:
        error_info = enhance_kiro_error(json.loads(text))
    except (json.JSONDecodeError, KeyError, TypeError):
        return "Kiro returned an upstream error response", None
    logger.debug(
        "Responses upstream error classified: reason={}, status={}",
        error_info.reason,
        response.status_code,
    )
    if error_info.reason == "CONTENT_LENGTH_EXCEEDS_THRESHOLD":
        return (
            error_info.user_message
            + " Reduce the client-owned history, images, or tool definitions and retry; "
            "KiroaaS does not trim or summarize Responses input.",
            error_info.reason,
        )
    return error_info.user_message, error_info.reason


def _error_response(status_code: int, message: str, error_type: str = "kiro_api_error") -> JSONResponse:
    """Build the public pre-stream error shape without echoing upstream bodies."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": status_code,
            }
        },
    )


async def _prepare_responses_stream(
    request: Request,
    request_data: ResponsesRequest,
    request_ir: ResponsesRequestIR,
    response_id: str,
) -> Union[_PreparedResponsesStream, JSONResponse]:
    """Prepare and prefetch a stream while ordinary HTTP errors are still possible.

    The first Kiro event is buffered before returning. This lets the route
    perform token refresh, generation retry, and account failover without
    emitting a Responses event that would make a retry ambiguous.
    """
    account_manager = request.app.state.account_manager
    account_system = bool(request.app.state.account_system)
    all_accounts = list(getattr(account_manager, "_accounts", {}))
    max_accounts = len(all_accounts) if account_system else 1
    tried_accounts: set[str] = set()
    last_error_message = "Kiro response generation failed before Responses started"
    last_error_status = 502

    for _ in range(max(max_accounts, 1)):
        account = await _select_account(
            request,
            request_data.model,
            exclude_accounts=tried_accounts if account_system else None,
        )
        if account_system:
            tried_accounts.add(account.id)
        if account.model_resolver is None:
            last_error_message = "The selected account has no model resolver"
            last_error_status = 503
            continue

        model_resolution = account.model_resolver.resolve(request_data.model)
        auth_manager = account.auth_manager
        if auth_manager is None:
            last_error_message = "The selected account has no Kiro authentication"
            last_error_status = 503
            continue
        profile_arn = auth_manager.profile_arn or PROFILE_ARN or ""
        try:
            payload_result = build_responses_kiro_payload(
                request_ir,
                profile_arn,
                model_resolution.internal_id,
            )
        except ResponsesConversionError as exc:
            if debug_logger:
                debug_logger.flush_on_error(400, str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        logger.info(
            "Responses stream accepted: response_id={} external_model={} kiro_model={} "
            "account={} items={} tools={} input_chars={}",
            response_id,
            request_data.model,
            model_resolution.internal_id,
            account.id,
            len(request_ir.items),
            len(request_ir.tools),
            sum(len(item.text) for item in request_ir.items),
        )

        http_client = KiroHttpClient(auth_manager)
        url = f"{auth_manager.api_host}/generateAssistantResponse"
        account_stream_succeeded = False
        account_should_failover = False

        for generation_attempt in range(max(FIRST_TOKEN_MAX_RETRIES, 1)):
            upstream_response: Optional[httpx.Response] = None
            parsed_stream: Optional[AsyncGenerator[KiroEvent, None]] = None
            try:
                if debug_logger:
                    debug_logger.log_kiro_request_body(
                        json.dumps(payload_result.payload, ensure_ascii=False).encode("utf-8")
                    )
                upstream_response = await http_client.request_with_retry(
                    "POST",
                    url,
                    payload_result.payload,
                    stream=True,
                )
                if upstream_response.status_code != 200:
                    message, reason = await _read_upstream_error(upstream_response)
                    error_type = classify_error(upstream_response.status_code, reason)
                    last_error_message = message
                    last_error_status = upstream_response.status_code
                    if account_system:
                        await account_manager.report_failure(
                            account.id,
                            request_data.model,
                            error_type,
                            upstream_response.status_code,
                            reason,
                        )
                    if account_system and error_type == ErrorType.RECOVERABLE:
                        account_should_failover = True
                        break
                    return _error_response(upstream_response.status_code, message)

                parsed_stream = parse_kiro_stream(
                    upstream_response,
                    first_token_timeout=FIRST_TOKEN_TIMEOUT,
                    deduplicate_result_tool_calls=False,
                    allow_legacy_json=False,
                )
                try:
                    first_event = await parsed_stream.__anext__()
                except StopAsyncIteration:
                    first_event = None
                state = ResponsesStreamState(
                    request=request_data,
                    request_ir=request_ir,
                    response_id=response_id,
                    model_cache=account.model_cache,
                )
                account_stream_succeeded = True
                return _PreparedResponsesStream(
                    account=account,
                    http_client=http_client,
                    upstream_response=upstream_response,
                    parsed_stream=parsed_stream,
                    first_event=first_event,
                    state=state,
                )
            except HTTPException as exc:
                last_error_message = "Kiro response stream failed before Responses started"
                last_error_status = exc.status_code
                logger.warning(
                    "Responses stream request failed: response_id={} status={} "
                    "attempt={}/{}",
                    response_id,
                    exc.status_code,
                    generation_attempt + 1,
                    max(FIRST_TOKEN_MAX_RETRIES, 1),
                )
                if generation_attempt + 1 == max(FIRST_TOKEN_MAX_RETRIES, 1):
                    account_should_failover = account_system
                    if account_system:
                        await account_manager.report_failure(
                            account.id,
                            request_data.model,
                            ErrorType.RECOVERABLE,
                            exc.status_code,
                            None,
                        )
                    elif debug_logger:
                        debug_logger.flush_on_error(exc.status_code, type(exc).__name__)
            except FirstTokenTimeoutError as exc:
                last_error_message = "Kiro did not start the Responses stream before the timeout"
                last_error_status = 504
                logger.warning(
                    "Responses stream prefetch timed out: response_id={} attempt={}/{}",
                    response_id,
                    generation_attempt + 1,
                    max(FIRST_TOKEN_MAX_RETRIES, 1),
                )
                if generation_attempt + 1 == max(FIRST_TOKEN_MAX_RETRIES, 1):
                    account_should_failover = account_system
                    if account_system:
                        await account_manager.report_failure(
                            account.id,
                            request_data.model,
                            ErrorType.RECOVERABLE,
                            504,
                            None,
                        )
                    elif debug_logger:
                        debug_logger.flush_on_error(504, str(exc))
            except (httpx.HTTPError, TimeoutError, ValueError, RuntimeError) as exc:
                last_error_message = "Kiro response stream failed before Responses started"
                last_error_status = 502
                logger.warning(
                    "Responses stream prefetch failed: response_id={} error_type={} attempt={}/{}",
                    response_id,
                    type(exc).__name__,
                    generation_attempt + 1,
                    max(FIRST_TOKEN_MAX_RETRIES, 1),
                )
                if generation_attempt + 1 == max(FIRST_TOKEN_MAX_RETRIES, 1):
                    account_should_failover = account_system
                    if account_system:
                        await account_manager.report_failure(
                            account.id,
                            request_data.model,
                            ErrorType.RECOVERABLE,
                            502,
                            None,
                        )
                    elif debug_logger:
                        debug_logger.flush_on_error(502, type(exc).__name__)
            finally:
                if not account_stream_succeeded:
                    await _close_upstream_resources(
                        http_client,
                        upstream_response,
                        parsed_stream,
                    )

        if account_should_failover:
            continue
        if not account_system:
            return _error_response(last_error_status, last_error_message)

    if account_system and len(all_accounts) > 1:
        last_error_status = 503
        last_error_message = "All available Kiro accounts failed before Responses started"
    if debug_logger:
        debug_logger.flush_on_error(last_error_status, last_error_message)
    return _error_response(last_error_status, last_error_message)


async def _stream_responses_body(
    request: Request,
    prepared: _PreparedResponsesStream,
) -> AsyncGenerator[str, None]:
    """Yield the prepared stream and always release request-scoped resources."""
    state = prepared.state
    try:
        if await request.is_disconnected():
            raise asyncio.CancelledError()
        yield encode_response_sse(state.created_event())
        if prepared.first_event is not None:
            for event in state.consume(prepared.first_event):
                yield encode_response_sse(event)
        async for kiro_event in prepared.parsed_stream:
            if await request.is_disconnected():
                raise asyncio.CancelledError()
            for event in state.consume(kiro_event):
                yield encode_response_sse(event)
        for event in state.complete():
            yield encode_response_sse(event)
        await request.app.state.account_manager.report_success(
            prepared.account.id,
            state.request.model,
        )
        if debug_logger:
            debug_logger.discard_buffers()
    except asyncio.CancelledError:
        logger.info("Responses stream cancelled by client: response_id={}", state.response_id)
        raise
    except (ResponsesConversionError, httpx.HTTPError, TimeoutError, ValueError, RuntimeError) as exc:
        for event in state.fail(exc):
            yield encode_response_sse(event)
        try:
            await request.app.state.account_manager.report_failure(
                prepared.account.id,
                state.request.model,
                ErrorType.RECOVERABLE,
                502,
                None,
            )
        except (RuntimeError, ValueError) as report_error:
            logger.warning(
                "Failed to record Responses stream failure: {}",
                type(report_error).__name__,
            )
        if debug_logger:
            debug_logger.flush_on_error(502, type(exc).__name__)
    finally:
        await _close_upstream_resources(
            prepared.http_client,
            prepared.upstream_response,
            prepared.parsed_stream,
        )


@router.post(
    "/v1/responses",
    dependencies=[Depends(verify_api_key)],
    response_model=None,
)
async def create_response(
    request: Request, request_data: ResponsesRequest
) -> Union[JSONResponse, StreamingResponse]:
    """Create one stateless Responses object or a complete SSE lifecycle.

    Args:
        request: FastAPI request containing account and HTTP client state.
        request_data: Independent Responses request model.

    Returns:
        A formal ``object=response`` JSON object or data-only Responses SSE.

    Raises:
        HTTPException: For validation, account, network, or upstream errors.
    """
    try:
        request_ir = convert_responses_request(request_data)
    except ResponsesConversionError as exc:
        if debug_logger:
            debug_logger.flush_on_error(400, str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response_id = generate_response_id()
    if request_data.stream:
        prepared = await _prepare_responses_stream(
            request,
            request_data,
            request_ir,
            response_id,
        )
        if isinstance(prepared, JSONResponse):
            return prepared
        return StreamingResponse(
            _stream_responses_body(request, prepared),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    account_manager = request.app.state.account_manager
    account_system = bool(request.app.state.account_system)
    all_accounts = list(getattr(account_manager, "_accounts", {}))
    max_attempts = len(all_accounts) if account_system else 1
    tried_accounts: set[str] = set()
    last_error_message = "Kiro response generation failed"
    last_error_status = 502

    for attempt in range(max(max_attempts, 1)):
        account = await _select_account(
            request,
            request_data.model,
            exclude_accounts=tried_accounts if account_system else None,
        )
        if account_system:
            tried_accounts.add(account.id)

        model_resolver = account.model_resolver
        if model_resolver is None:
            if not account_system:
                raise HTTPException(
                    status_code=503,
                    detail="The selected account has no model resolver",
                )
            last_error_message = "The selected account has no model resolver"
            last_error_status = 503
            continue

        model_resolution = model_resolver.resolve(request_data.model)
        auth_manager = account.auth_manager
        profile_arn = auth_manager.profile_arn or PROFILE_ARN or ""
        try:
            payload_result = build_responses_kiro_payload(
                request_ir,
                profile_arn,
                model_resolution.internal_id,
            )
        except ResponsesConversionError as exc:
            if debug_logger:
                debug_logger.flush_on_error(400, str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info(
            "Responses request accepted: response_id={} external_model={} kiro_model={} "
            "account={} items={} tools={} input_chars={}",
            response_id,
            request_data.model,
            model_resolution.internal_id,
            account.id,
            len(request_ir.items),
            len(request_ir.tools),
            sum(len(item.text) for item in request_ir.items),
        )

        url = f"{auth_manager.api_host}/generateAssistantResponse"
        # The public endpoint buffers the stream, but the upstream request remains
        # streaming and therefore owns a request-scoped client.
        http_client = KiroHttpClient(auth_manager)
        upstream_response: Optional[httpx.Response] = None
        try:
            if debug_logger:
                debug_logger.log_kiro_request_body(
                    json.dumps(payload_result.payload, ensure_ascii=False).encode("utf-8")
                )
            upstream_response = await http_client.request_with_retry(
                "POST",
                url,
                payload_result.payload,
                stream=True,
            )
            if upstream_response.status_code != 200:
                message, reason = await _read_upstream_error(upstream_response)
                error_type = classify_error(upstream_response.status_code, reason)
                last_error_message = message
                last_error_status = upstream_response.status_code
                if account_system:
                    await account_manager.report_failure(
                        account.id,
                        request_data.model,
                        error_type,
                        upstream_response.status_code,
                        reason,
                    )
                if account_system and error_type == ErrorType.RECOVERABLE:
                    if attempt + 1 < max_attempts:
                        continue
                    break
                if debug_logger:
                    debug_logger.flush_on_error(upstream_response.status_code, message)
                return JSONResponse(
                    status_code=upstream_response.status_code,
                    content={
                        "error": {
                            "message": message,
                            "type": "kiro_api_error",
                            "code": upstream_response.status_code,
                        }
                    },
                )

            stream_result = await collect_stream_to_result(
                upstream_response,
                deduplicate_result_tool_calls=False,
                allow_legacy_json=False,
            )
            try:
                response_body = build_responses_object(
                    request_data,
                    request_ir,
                    stream_result,
                    account.model_cache,
                    response_id=response_id,
                )
            except ResponsesConversionError as exc:
                logger.error(
                    "Responses upstream Tool Call protocol error: response_id={} error={}",
                    response_id,
                    exc,
                )
                if debug_logger:
                    debug_logger.flush_on_error(502, str(exc))
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": {
                            "message": str(exc),
                            "type": "kiro_protocol_error",
                            "code": 502,
                        }
                    },
                )
            await account_manager.report_success(account.id, request_data.model)
            if debug_logger:
                debug_logger.discard_buffers()
            logger.info(
                "Responses request completed: response_id={} output_items={} status=completed",
                response_id,
                len(response_body["output"]),
            )
            return JSONResponse(content=response_body)
        except HTTPException as exc:
            if account_system and exc.status_code in (502, 504):
                await account_manager.report_failure(
                    account.id,
                    request_data.model,
                    ErrorType.RECOVERABLE,
                    exc.status_code,
                    None,
                )
                last_error_message = "Kiro response collection failed before completion"
                last_error_status = exc.status_code
                if attempt + 1 < max_attempts:
                    continue
                break
            if debug_logger:
                debug_logger.flush_on_error(exc.status_code, str(exc.detail))
            raise
        except (httpx.HTTPError, TimeoutError, FirstTokenTimeoutError) as exc:
            logger.error("Responses upstream collection failed: {}", type(exc).__name__)
            last_error_message = "Kiro response collection failed before completion"
            last_error_status = 502
            if account_system:
                await account_manager.report_failure(
                    account.id,
                    request_data.model,
                    ErrorType.RECOVERABLE,
                    502,
                    None,
                )
                if attempt + 1 < max_attempts:
                    continue
                break
            if debug_logger:
                debug_logger.flush_on_error(502, type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail="Kiro response collection failed before a Responses object was completed",
            ) from exc
        finally:
            if upstream_response is not None:
                try:
                    await upstream_response.aclose()
                except httpx.HTTPError:
                    logger.debug("Failed to close Responses upstream response")
            await http_client.close()

    if account_system and len(all_accounts) > 1:
        last_error_status = 503
        last_error_message = "All available Kiro accounts failed before a Responses object was completed"
    if debug_logger:
        debug_logger.flush_on_error(last_error_status, last_error_message)
    return JSONResponse(
        status_code=last_error_status,
        content={
            "error": {
                "message": last_error_message,
                "type": "kiro_api_error",
                "code": last_error_status,
            }
        },
    )
