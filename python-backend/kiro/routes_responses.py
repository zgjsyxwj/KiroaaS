# -*- coding: utf-8 -*-

"""FastAPI route for the stateless non-streaming Responses provider."""

import json
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

from kiro.account_errors import ErrorType, classify_error
from kiro.config import PROFILE_ARN
from kiro.converters_responses import (
    ResponsesConversionError,
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
from kiro.streaming_core import FirstTokenTimeoutError, collect_stream_to_result

try:
    from kiro.debug_logger import debug_logger
except ImportError:
    debug_logger = None


router = APIRouter()


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


@router.post("/v1/responses", dependencies=[Depends(verify_api_key)])
async def create_response(request: Request, request_data: ResponsesRequest) -> JSONResponse:
    """Create one stateless, non-streaming Responses object.

    Args:
        request: FastAPI request containing account and HTTP client state.
        request_data: Independent Responses request model.

    Returns:
        A formal ``object=response`` JSON object.

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
