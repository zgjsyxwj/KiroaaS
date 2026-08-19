# -*- coding: utf-8 -*-

"""Pydantic models for the non-streaming Responses API surface."""

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


class ResponsesInputItem(BaseModel):
    """A Responses input item accepted by the first stateless provider path.

    The model intentionally stays separate from ``ChatMessage``. Responses
    input items include replayable function calls and tool results that do not
    have a faithful representation in the Chat Completions request model.
    """

    type: str = "message"
    role: Optional[str] = None
    content: Any = None
    id: Optional[str] = None
    status: Optional[str] = None
    call_id: Optional[str] = None
    name: Optional[str] = None
    arguments: Any = None
    input: Any = None
    action: Any = None
    operation: Any = None
    execution: Optional[str] = None
    tools: Any = None
    caller: Any = None
    environment: Any = None
    namespace: Optional[str] = None
    output: Any = None
    summary: Any = None
    encrypted_content: Any = None
    encrypted_text: Any = None

    model_config = {"extra": "forbid"}


class ResponsesRequest(BaseModel):
    """Request model for ``POST /v1/responses``.

    Only response creation is implemented. Stateful and hosted capabilities
    remain explicit fields so the route can reject them with an actionable
    error instead of silently dropping them.
    """

    model: str
    input: Union[str, List[ResponsesInputItem]]
    instructions: Optional[Union[str, List[Dict[str, Any]]]] = None
    stream: bool = False
    store: Optional[bool] = None
    previous_response_id: Optional[str] = None
    conversation: Any = None
    background: Optional[bool] = None
    service_tier: Optional[str] = None

    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Any = None
    parallel_tool_calls: Optional[bool] = None

    reasoning: Optional[Dict[str, Any]] = None
    max_output_tokens: Optional[int] = Field(default=None, ge=1)
    max_tool_calls: Optional[int] = Field(default=None, ge=1)
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    text: Optional[Dict[str, Any]] = None
    truncation: Optional[str] = None

    include: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    prompt: Optional[Dict[str, Any]] = None
    prompt_cache_key: Optional[str] = None
    prompt_cache_retention: Optional[str] = None
    safety_identifier: Optional[str] = None
    user: Optional[str] = None

    model_config = {"extra": "forbid"}
