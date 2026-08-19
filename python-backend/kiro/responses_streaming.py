# -*- coding: utf-8 -*-

"""Responses SSE lifecycle state machine for one Kiro generation."""

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger

from kiro.converters_responses import ResponsesConversionError, ResponsesRequestIR
from kiro.models_responses import ResponsesRequest
from kiro.responses_provider import (
    _build_client_tool_output,
    _new_item_id,
    _new_request_scoped_call_id,
    _parse_kiro_tool_arguments,
    estimate_responses_usage,
)
from kiro.streaming_core import KiroEvent, StreamResult


def encode_response_sse(event: Dict[str, Any]) -> str:
    """Encode one Responses event as a data-only SSE record."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@dataclass
class ResponsesStreamState:
    """Own identities, ordering, output items, and usage for one response."""

    request: ResponsesRequest
    request_ir: ResponsesRequestIR
    response_id: str
    model_cache: Optional[Any]
    _sequence_number: int = 0
    _created_at: int = field(default_factory=lambda: int(time.time()))
    _output: List[Dict[str, Any]] = field(default_factory=list)
    _next_output_index: int = 0
    _text_item: Optional[Dict[str, Any]] = None
    _text_output_index: Optional[int] = None
    _tool_items: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _result: StreamResult = field(default_factory=StreamResult)
    _created: bool = False
    _terminal: bool = False

    def created_event(self) -> Dict[str, Any]:
        """Return the single first event that establishes response identity."""
        if self._created:
            raise RuntimeError("Responses response.created was already emitted")
        self._created = True
        return self._event(
            "response.created",
            response=self._response_snapshot("in_progress", None, None),
        )

    def consume(self, event: KiroEvent) -> List[Dict[str, Any]]:
        """Translate one parsed Kiro event into ordered Responses events."""
        if self._terminal:
            return []
        if event.type == "content" and event.content:
            self._result.content += event.content
            return self._consume_text(event.content)
        if event.type == "thinking" and event.thinking_content:
            self._result.thinking_content += event.thinking_content
            return []
        if event.type == "usage" and event.usage is not None:
            self._result.usage = event.usage
            return []
        if event.type == "context_usage" and event.context_usage_percentage is not None:
            self._result.context_usage_percentage = event.context_usage_percentage
            return []
        if event.type == "tool_use" and event.tool_use:
            return self._consume_tool(event.tool_use)
        return []

    def complete(self) -> List[Dict[str, Any]]:
        """Close output items and emit exactly one completed terminal event."""
        if self._terminal:
            return []
        events = self._finish_text()
        usage = estimate_responses_usage(
            self._result,
            self.request_ir,
            self.model_cache,
            self.request.model,
        )
        self._terminal = True
        events.append(
            self._event(
                "response.completed",
                response=self._response_snapshot("completed", usage.as_dict(), None),
            )
        )
        logger.info(
            "Responses stream completed: response_id={} output_items={} "
            "input_measurement={} output_measurement={} total_measurement={}",
            self.response_id,
            len(self._output),
            usage.input_source,
            usage.output_source,
            usage.total_source,
        )
        return events

    def fail(self, error: BaseException) -> List[Dict[str, Any]]:
        """Emit one sanitized terminal failure after response.created."""
        if not self._created or self._terminal:
            return []
        usage = estimate_responses_usage(
            self._result,
            self.request_ir,
            self.model_cache,
            self.request.model,
        )
        error_message = self._safe_error_message(error)
        self._terminal = True
        logger.error(
            "Responses stream failed: response_id={} error_type={} "
            "input_measurement={} output_measurement={} total_measurement={}",
            self.response_id,
            type(error).__name__,
            usage.input_source,
            usage.output_source,
            usage.total_source,
        )
        return [
            self._event(
                "response.failed",
                response=self._response_snapshot(
                    "failed",
                    usage.as_dict(),
                    {"code": "stream_error", "message": error_message},
                ),
            )
        ]

    def _consume_text(self, delta: str) -> List[Dict[str, Any]]:
        """Open the text item once and emit exactly one event per text delta."""
        events: List[Dict[str, Any]] = []
        if self._text_item is None:
            self._text_output_index = self._allocate_output_index()
            self._text_item = {
                "id": _new_item_id("msg"),
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            self._text_item["content"].append(
                {"type": "output_text", "text": "", "annotations": []}
            )
            self._output.append(self._text_item)
            events.append(
                self._event(
                    "response.output_item.added",
                    output_index=self._text_output_index,
                    item={**copy.deepcopy(self._text_item), "content": []},
                )
            )
            events.append(
                self._event(
                    "response.content_part.added",
                    item_id=self._text_item["id"],
                    output_index=self._text_output_index,
                    content_index=0,
                    part=copy.deepcopy(self._text_item["content"][0]),
                )
            )

        part = self._text_item["content"][0]
        part["text"] += delta
        events.append(
            self._event(
                "response.output_text.delta",
                item_id=self._text_item["id"],
                output_index=self._text_output_index,
                content_index=0,
                delta=delta,
                logprobs=[],
            )
        )
        return events

    def _finish_text(self) -> List[Dict[str, Any]]:
        """Emit text done, content part done, and item done in that order."""
        if self._text_item is None:
            return []
        item = self._text_item
        part = item["content"][0]
        item["status"] = "completed"
        events = [
            self._event(
                "response.output_text.done",
                item_id=item["id"],
                output_index=self._text_output_index,
                content_index=0,
                text=part["text"],
                logprobs=[],
            ),
            self._event(
                "response.content_part.done",
                item_id=item["id"],
                output_index=self._text_output_index,
                content_index=0,
                part=copy.deepcopy(part),
            ),
            self._event(
                "response.output_item.done",
                output_index=self._text_output_index,
                item=copy.deepcopy(item),
            ),
        ]
        self._text_item = None
        return events

    def _consume_tool(self, tool: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Emit a complete Client Tool Call lifecycle from one Kiro tool event."""
        function = tool.get("function") or {}
        name = function.get("name") or tool.get("name")
        if not isinstance(name, str) or not name:
            raise ResponsesConversionError("Kiro returned a Tool Call without a function name")
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, (dict, list)):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if not isinstance(arguments, str):
            raise ResponsesConversionError("Kiro returned malformed Tool Call arguments")
        call_id = tool.get("id")
        if tool.get("_generated_id") or not isinstance(call_id, str) or not call_id:
            call_id = _new_request_scoped_call_id(
                self.response_id, self._next_output_index, set(self._tool_items)
            )
        if call_id in self._tool_items:
            raise ResponsesConversionError(
                f"Kiro returned duplicate Tool Call call_id '{call_id}'"
            )
        parsed_arguments = _parse_kiro_tool_arguments(arguments, call_id)
        registration = self.request_ir.tool_registry.resolve_or_bind(call_id, name)
        item_id = _new_item_id("fc")
        output_index = self._allocate_output_index()
        completed_item = _build_client_tool_output(
            registration,
            item_id,
            call_id,
            name,
            arguments,
            parsed_arguments,
        )
        item = copy.deepcopy(completed_item)
        item["status"] = "in_progress"
        if item["type"] == "function_call":
            item["arguments"] = ""
        elif item["type"] == "custom_tool_call":
            item["input"] = ""
        self._output.append(item)
        self._tool_items[call_id] = item
        self._result.tool_calls.append(tool)
        events = [
            *self._finish_text(),
            self._event(
                "response.output_item.added",
                output_index=output_index,
                item=copy.deepcopy(item),
            ),
        ]
        if item["type"] == "function_call":
            events.extend(
                [
                    self._event(
                        "response.function_call_arguments.delta",
                        item_id=item_id,
                        output_index=output_index,
                        delta=arguments,
                    ),
                    self._event(
                        "response.function_call_arguments.done",
                        item_id=item_id,
                        output_index=output_index,
                        arguments=arguments,
                    ),
                ]
            )
        item["status"] = "completed"
        if item["type"] == "function_call":
            item["arguments"] = arguments
        elif item["type"] == "custom_tool_call":
            item["input"] = parsed_arguments["input"]
        events.append(
            self._event(
                "response.output_item.done",
                output_index=output_index,
                item=copy.deepcopy(item),
            )
        )
        return events

    def _response_snapshot(
        self,
        status: str,
        usage: Optional[Dict[str, Any]],
        error: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Build a response object with stable request and output identities."""
        effort = (self.request.reasoning or {}).get("effort")
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self._created_at,
            "status": status,
            "background": False,
            "completed_at": int(time.time()) if status != "in_progress" else None,
            "error": error,
            "incomplete_details": None,
            "input": [item.model_dump(exclude_none=True) for item in self.request.input]
            if isinstance(self.request.input, list)
            else self.request.input,
            "instructions": self.request.instructions,
            "max_output_tokens": self.request.max_output_tokens,
            "max_tool_calls": self.request.max_tool_calls,
            "model": self.request.model,
            "output": copy.deepcopy(self._output),
            "parallel_tool_calls": self.request.parallel_tool_calls
            if self.request.parallel_tool_calls is not None
            else True,
            "previous_response_id": None,
            "prompt": None,
            "prompt_cache_key": self.request.prompt_cache_key,
            "reasoning": {"effort": effort, "summary": None},
            "reasoning_effort": effort,
            "safety_identifier": self.request.safety_identifier,
            "service_tier": None,
            "store": False,
            "temperature": self.request.temperature
            if self.request.temperature is not None
            else 1.0,
            "text": self.request.text or {"format": {"type": "text"}},
            "tool_choice": self.request.tool_choice or "auto",
            "tools": self.request.tools or [],
            "top_logprobs": None,
            "top_p": self.request.top_p if self.request.top_p is not None else 1.0,
            "truncation": "disabled",
            "usage": usage,
            "user": self.request.user,
            "metadata": self.request.metadata or {},
        }

    def _event(self, event_type: str, **fields: Any) -> Dict[str, Any]:
        """Assign the next response-local sequence number to one event."""
        event = {"type": event_type, "sequence_number": self._sequence_number}
        self._sequence_number += 1
        event.update(fields)
        return event

    def _allocate_output_index(self) -> int:
        """Return the next stable output index."""
        output_index = self._next_output_index
        self._next_output_index += 1
        return output_index

    @staticmethod
    def _safe_error_message(error: BaseException) -> str:
        """Return an actionable but body-free terminal error message."""
        if isinstance(error, ResponsesConversionError):
            return str(error)
        return "Kiro response generation failed after response.created"
