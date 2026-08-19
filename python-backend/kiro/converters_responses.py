# -*- coding: utf-8 -*-

"""Convert Responses requests into a protocol-specific intermediate form."""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from kiro.converters_core import (
    ThinkingConfig,
    UnifiedMessage,
    UnifiedTool,
    extract_images_from_content,
    extract_text_content,
)
from kiro.models_responses import ResponsesInputItem, ResponsesRequest


THINKING_BUDGETS: Dict[str, int] = {
    "minimal": 1024,
    "low": 4096,
    "medium": 12000,
    "high": 20000,
    "xhigh": 22528,
    "max": 24576,
}

_HOSTED_TOOL_TYPES = {
    "computer_use_preview",
    "file_search",
    "image_generation",
    "mcp",
    "web_search",
    "web_search_preview",
}


class ResponsesConversionError(ValueError):
    """Raised when a Responses capability cannot be represented faithfully."""


@dataclass(frozen=True)
class ResponsesInputItemIR:
    """Protocol-preserving representation of one Responses input item."""

    item_type: str
    role: Optional[str]
    item_id: Optional[str]
    call_id: Optional[str]
    content: Any
    text: str
    images: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_result: Optional[str] = None


@dataclass(frozen=True)
class ResponsesToolIR:
    """Protocol-preserving representation of a registered client tool."""

    external_type: str
    name: str
    description: Optional[str]
    parameters: Dict[str, Any]

    def as_unified_tool(self) -> UnifiedTool:
        """Return the Kiro-neutral tool representation."""
        return UnifiedTool(
            name=self.name,
            description=self.description,
            input_schema=self.parameters,
        )


@dataclass(frozen=True)
class ResponsesRequestIR:
    """Complete Responses IR used by the Kiro payload and output adapters."""

    external_model_id: str
    items: List[ResponsesInputItemIR]
    tools: List[ResponsesToolIR]
    raw_tools: List[Dict[str, Any]]
    instruction_segments: List[str]
    system_prompt: str
    messages: List[UnifiedMessage]
    tokenizer_messages: List[Dict[str, Any]]
    thinking_config: ThinkingConfig

    @property
    def unified_tools(self) -> Optional[List[UnifiedTool]]:
        """Return registered tools for the shared Kiro converter."""
        tools = [tool.as_unified_tool() for tool in self.tools]
        return tools or None


def _extract_instruction_text(value: Any, field_name: str) -> str:
    """Extract text from an instructions field or message content."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                raise ResponsesConversionError(
                    f"{field_name} must contain text blocks"
                )
            block_type = block.get("type", "input_text")
            if block_type not in {"input_text", "text", "output_text", "summary_text"}:
                raise ResponsesConversionError(
                    f"Unsupported {field_name} content type: {block_type}"
                )
            text = block.get("text")
            if not isinstance(text, str):
                raise ResponsesConversionError(
                    f"{field_name} text blocks require a string 'text'"
                )
            parts.append(text)
        return "".join(parts)
    raise ResponsesConversionError(f"{field_name} must be a string or text blocks")


def _normalize_content(content: Any, field_name: str) -> Tuple[str, List[Dict[str, Any]], Any]:
    """Normalize Responses content while accepting only local media.

    Base64 data URLs are converted to the image block shape understood by the
    shared converter. Remote URLs, file IDs and file URLs are rejected before
    any network-capable code can see them.
    """
    if isinstance(content, str) or content is None:
        return content or "", [], content

    if not isinstance(content, list):
        raise ResponsesConversionError(f"{field_name} must be a string or item list")

    normalized_blocks: List[Dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            normalized_blocks.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            raise ResponsesConversionError(f"{field_name} contains an invalid block")

        block_type = block.get("type")
        if block_type in {"input_text", "output_text", "text"}:
            text = block.get("text")
            if not isinstance(text, str):
                raise ResponsesConversionError(
                    f"{field_name} text blocks require a string 'text'"
                )
            normalized_blocks.append({"type": "text", "text": text})
            continue

        if block_type in {"input_image", "image_url", "image"}:
            image_block = _normalize_image_block(block, field_name)
            normalized_blocks.append(image_block)
            continue

        raise ResponsesConversionError(
            f"Unsupported {field_name} content type: {block_type or 'missing type'}"
        )

    text = extract_text_content(normalized_blocks)
    images = extract_images_from_content(normalized_blocks)
    return text, images, normalized_blocks


def _normalize_image_block(block: Dict[str, Any], field_name: str) -> Dict[str, Any]:
    """Convert a supported Responses image block to the shared image shape."""
    block_type = block.get("type")
    if block_type == "image":
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "base64":
            media_type = source.get("media_type")
            data = source.get("data")
            if isinstance(media_type, str) and isinstance(data, str) and data:
                return {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
        raise ResponsesConversionError(
            f"{field_name} only supports base64 image sources"
        )

    image_value = block.get("image_url")
    if isinstance(image_value, dict):
        image_url = image_value.get("url")
    else:
        image_url = image_value
    if not isinstance(image_url, str) or not image_url.startswith("data:"):
        raise ResponsesConversionError(
            f"{field_name} only supports base64 data URL images"
        )
    return {"type": "image_url", "image_url": {"url": image_url}}


def _convert_tool_call(item: ResponsesInputItem) -> Dict[str, Any]:
    """Convert a replayed Responses function call to unified tool-call form."""
    if not item.call_id or not item.name:
        raise ResponsesConversionError(
            "function_call items require both call_id and name"
        )
    arguments = item.arguments if item.arguments is not None else "{}"
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": item.call_id,
        "type": "function",
        "function": {"name": item.name, "arguments": arguments},
    }


def _convert_tool_result(item: ResponsesInputItem) -> Dict[str, Any]:
    """Convert a replayed Responses function output to unified tool-result form."""
    if not item.call_id:
        raise ResponsesConversionError("function_call_output items require call_id")
    output = item.output
    if output is None:
        output = item.content
    if isinstance(output, (dict, list)):
        output_text = json.dumps(output, ensure_ascii=False)
    elif output is None:
        output_text = ""
    else:
        output_text = str(output)
    return {
        "type": "tool_result",
        "tool_use_id": item.call_id,
        "content": output_text or "(empty result)",
    }


def _convert_input_item(item: ResponsesInputItem) -> ResponsesInputItemIR:
    """Convert one Pydantic item to the protocol IR."""
    item_type = item.type or "message"

    if item_type == "function_call":
        call = _convert_tool_call(item)
        return ResponsesInputItemIR(
            item_type=item_type,
            role="assistant",
            item_id=item.id,
            call_id=item.call_id,
            content=item.content,
            text="",
            tool_calls=[call],
        )

    if item_type == "function_call_output":
        result = _convert_tool_result(item)
        return ResponsesInputItemIR(
            item_type=item_type,
            role="user",
            item_id=item.id,
            call_id=item.call_id,
            content=item.output if item.output is not None else item.content,
            text=result["content"],
            tool_result=result["content"],
        )

    if item_type == "reasoning":
        item_data = item.model_dump()
        summary = item_data.get("summary")
        if isinstance(summary, list):
            # Encrypted summaries are intentionally unavailable at this
            # boundary. Preserve readable summary_text blocks and ignore the
            # encrypted blocks without fabricating assistant content.
            readable_summary = [
                block
                for block in summary
                if not (
                    isinstance(block, dict)
                    and (
                        "encrypted_content" in block
                        or "encrypted_text" in block
                        or "encrypted" in str(block.get("type", ""))
                    )
                )
            ]
            text = _extract_instruction_text(readable_summary, "reasoning")
        elif summary is not None:
            text = _extract_instruction_text(summary, "reasoning")
        elif "encrypted_content" in item_data or "encrypted_text" in item_data:
            text = ""
        else:
            text = _extract_instruction_text(item.content, "reasoning")
        return ResponsesInputItemIR(
            item_type=item_type,
            role="assistant",
            item_id=item.id,
            call_id=item.call_id,
            content=item.content,
            text=text,
        )

    if item_type != "message":
        raise ResponsesConversionError(f"Unsupported Responses input item type: {item_type}")
    if item.role not in {"user", "assistant", "system", "developer"}:
        raise ResponsesConversionError(
            "message items require role user, assistant, system, or developer"
        )

    text, images, normalized_content = _normalize_content(
        item.content, f"message item {item.id or '<anonymous>'} content"
    )
    return ResponsesInputItemIR(
        item_type=item_type,
        role=item.role,
        item_id=item.id,
        call_id=item.call_id,
        content=normalized_content,
        text=text,
        images=images,
    )


def _convert_tools(tools: Optional[List[Dict[str, Any]]]) -> List[ResponsesToolIR]:
    """Convert supported client tools and reject hosted tools explicitly."""
    converted: List[ResponsesToolIR] = []
    for tool in tools or []:
        tool_type = tool.get("type")
        if tool_type in _HOSTED_TOOL_TYPES:
            raise ResponsesConversionError(
                f"Hosted Tool '{tool_type}' is not supported by KiroaaS Responses"
            )
        if tool_type != "function":
            raise ResponsesConversionError(
                f"Unsupported Responses tool type: {tool_type or 'missing type'}"
            )

        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ResponsesConversionError("function tools require a non-empty name")
        parameters = function.get("parameters", function.get("input_schema", {}))
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise ResponsesConversionError(f"Tool '{name}' parameters must be an object")
        converted.append(
            ResponsesToolIR(
                external_type="function",
                name=name,
                description=function.get("description"),
                parameters=parameters,
            )
        )
    return converted


def _extract_reasoning_config(request: ResponsesRequest) -> ThinkingConfig:
    """Map Responses reasoning effort to the documented Thinking Budget."""
    if not request.reasoning:
        return ThinkingConfig()
    effort = request.reasoning.get("effort")
    if effort is None:
        return ThinkingConfig()
    if effort == "none":
        return ThinkingConfig(enabled=False)
    if effort not in THINKING_BUDGETS:
        allowed = ", ".join(["none", *THINKING_BUDGETS.keys()])
        raise ResponsesConversionError(
            f"Unsupported reasoning effort '{effort}'. Allowed values: {allowed}"
        )
    return ThinkingConfig(enabled=True, budget_tokens=THINKING_BUDGETS[effort])


def _validate_request_capabilities(request: ResponsesRequest) -> None:
    """Reject stateful or contract-changing capabilities not implemented yet."""
    if request.stream:
        raise ResponsesConversionError(
            "Streaming Responses is not implemented; set stream=false"
        )
    if request.store is True:
        raise ResponsesConversionError("Responses storage is not supported; omit store or set store=false")
    if request.previous_response_id:
        raise ResponsesConversionError("previous_response_id is not supported; resend the full input")
    if request.conversation:
        raise ResponsesConversionError("Stateful conversations are not supported")
    if request.prompt:
        raise ResponsesConversionError("Prompt templates are not supported; send full input")
    if request.background is True:
        raise ResponsesConversionError("background Responses are not supported")
    if request.service_tier is not None:
        raise ResponsesConversionError("service_tier guarantees are not supported")
    if request.truncation not in {None, "disabled"}:
        raise ResponsesConversionError("Only truncation=disabled is supported")
    if request.text:
        text_format = request.text.get("format")
        if text_format is not None and text_format != {"type": "text"}:
            raise ResponsesConversionError("Structured output formats are not supported")
    if request.tool_choice is not None and request.tool_choice != "auto":
        raise ResponsesConversionError("Only automatic client tool choice is supported")
    if request.parallel_tool_calls is False:
        raise ResponsesConversionError(
            "parallel_tool_calls=false is not supported by the Kiro Responses adapter"
        )
    unsupported_controls = {
        "max_output_tokens": request.max_output_tokens,
        "max_tool_calls": request.max_tool_calls,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "prompt_cache_retention": request.prompt_cache_retention,
    }
    unsupported_names = [
        name for name, value in unsupported_controls.items() if value is not None
    ]
    if unsupported_names:
        raise ResponsesConversionError(
            "Unsupported Responses controls: " + ", ".join(unsupported_names)
        )
    if request.include:
        unsupported_include = [
            value
            for value in request.include
            if value != "reasoning.encrypted_content"
        ]
        if unsupported_include:
            raise ResponsesConversionError(
                "Unsupported Responses include values: "
                + ", ".join(unsupported_include)
            )
        logger.debug(
            "Responses encrypted reasoning inclusion requested; unavailable content omitted"
        )
    if request.text:
        unsupported_text_fields = set(request.text) - {"format"}
        if unsupported_text_fields:
            raise ResponsesConversionError(
                "Unsupported Responses text fields: "
                + ", ".join(sorted(unsupported_text_fields))
            )


def convert_responses_request(request: ResponsesRequest) -> ResponsesRequestIR:
    """Convert a Responses request into a Kiro-neutral protocol IR.

    ``instructions`` is placed before system/developer message content and
    each segment is added once. Kiro has no equivalent priority hierarchy, so
    this is deliberately a best-effort approximation documented at the
    provider boundary.

    Args:
        request: Validated, independent Responses request model.

    Returns:
        Protocol-specific IR retaining external item and tool identities.

    Raises:
        ResponsesConversionError: If a requested capability cannot be
            represented without changing Responses semantics.
    """
    _validate_request_capabilities(request)

    if isinstance(request.input, str):
        source_items = [
            ResponsesInputItem(type="message", role="user", content=request.input)
        ]
    else:
        source_items = request.input
    items = [_convert_input_item(item) for item in source_items]
    if not items:
        raise ResponsesConversionError("input must contain at least one item")

    known_call_ids = set()
    for item in items:
        if item.item_type == "function_call" and item.call_id:
            if item.call_id in known_call_ids:
                raise ResponsesConversionError(
                    f"Duplicate function_call call_id: {item.call_id}"
                )
            known_call_ids.add(item.call_id)
        elif item.item_type == "function_call_output":
            if item.call_id not in known_call_ids:
                raise ResponsesConversionError(
                    "function_call_output has no preceding function_call for call_id "
                    f"{item.call_id}"
                )

    instruction_segments: List[str] = []
    instructions_text = _extract_instruction_text(request.instructions, "instructions")
    if instructions_text:
        instruction_segments.append(instructions_text)
    for item in items:
        if item.role in {"system", "developer"} and item.text:
            instruction_segments.append(item.text)
    system_prompt = "\n\n".join(instruction_segments)

    messages: List[UnifiedMessage] = []
    tokenizer_messages: List[Dict[str, Any]] = []
    for item in items:
        if item.item_type == "reasoning" and not item.text:
            continue
        if item.role in {"system", "developer"}:
            continue
        message = UnifiedMessage(
            role=item.role or "user",
            content=item.content if item.item_type == "message" else item.text,
            tool_calls=item.tool_calls or None,
            tool_results=(
                [{
                    "type": "tool_result",
                    "tool_use_id": item.call_id or "",
                    "content": item.tool_result or "(empty result)",
                }]
                if item.item_type == "function_call_output"
                else None
            ),
            images=item.images or None,
        )
        messages.append(message)
        tokenizer_message: Dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }
        if message.tool_calls:
            tokenizer_message["tool_calls"] = message.tool_calls
        if message.tool_results:
            tokenizer_message["content"] = [
                {
                    "type": "tool_result",
                    "tool_use_id": item.call_id or "",
                    "content": item.tool_result or "(empty result)",
                }
            ]
        tokenizer_messages.append(tokenizer_message)

    if not messages:
        raise ResponsesConversionError("input must contain a user or assistant message")

    response_tools = _convert_tools(request.tools)
    logger.debug(
        "Responses IR: model={}, items={}, messages={}, tools={}, instruction_segments={} "
        "(instruction hierarchy is best-effort)",
        request.model,
        len(items),
        len(messages),
        len(response_tools),
        len(instruction_segments),
    )
    return ResponsesRequestIR(
        external_model_id=request.model,
        items=items,
        tools=response_tools,
        raw_tools=request.tools or [],
        instruction_segments=instruction_segments,
        system_prompt=system_prompt,
        messages=messages,
        tokenizer_messages=tokenizer_messages,
        thinking_config=_extract_reasoning_config(request),
    )
