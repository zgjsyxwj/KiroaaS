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
    "code_interpreter",
    "computer",
    "computer_use_preview",
    "file_search",
    "image_generation",
    "mcp",
    "remote_mcp",
    "web_search",
    "web_search_preview",
}

_SUPPORTED_CLIENT_TOOL_TYPES = (
    "function",
    "custom",
    "shell",
    "local_shell",
    "tool_search",
    "apply_patch",
)

_CLIENT_CALL_ITEM_TYPES = {
    "function_call": "function",
    "custom_tool_call": "custom",
    "shell_call": "shell",
    "local_shell_call": "local_shell",
    "tool_search_call": "tool_search",
    "apply_patch_call": "apply_patch",
}

_CLIENT_RESULT_ITEM_TYPES = {
    "function_call_output": "function",
    "custom_tool_call_output": "custom",
    "shell_call_output": "shell",
    "local_shell_call_output": "local_shell",
    "tool_search_output": "tool_search",
    "apply_patch_call_output": "apply_patch",
}

_DEFAULT_TOOL_NAMES = {
    "shell": "shell",
    "local_shell": "local_shell",
    "tool_search": "tool_search",
    "apply_patch": "apply_patch",
}

_CUSTOM_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {"input": {"type": "string"}},
    "required": ["input"],
}

_VERBOSITY_VALUES = {"low", "medium", "high"}


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
    tool_type: Optional[str] = None


@dataclass(frozen=True)
class ResponsesToolIR:
    """Protocol-preserving representation of a registered client tool."""

    external_type: str
    name: str
    description: Optional[str]
    parameters: Dict[str, Any]
    execution: Optional[str] = None

    def as_unified_tool(self) -> UnifiedTool:
        """Return the Kiro-neutral tool representation."""
        return UnifiedTool(
            name=self.name,
            description=self.description,
            input_schema=self.parameters,
        )


@dataclass
class ResponsesToolRegistry:
    """Track registered Client Tools by name and Tool Call identity."""

    by_name: Dict[str, ResponsesToolIR] = field(default_factory=dict)
    by_call_id: Dict[str, ResponsesToolIR] = field(default_factory=dict)

    def register(self, tool: ResponsesToolIR) -> None:
        """Register one tool definition, rejecting duplicate names."""
        if tool.name in self.by_name:
            existing = self.by_name[tool.name]
            if existing.external_type != tool.external_type:
                raise ResponsesConversionError(
                    f"Tool type conflict for name '{tool.name}': "
                    f"already registered as {existing.external_type}, "
                    f"cannot register {tool.external_type}"
                )
            raise ResponsesConversionError(
                f"Duplicate Client Tool registration for name '{tool.name}'"
            )
        self.by_name[tool.name] = tool

    def bind_call(self, call_id: str, tool: ResponsesToolIR) -> None:
        """Bind a Tool Call identity to its registered Client Tool."""
        existing = self.by_call_id.get(call_id)
        if existing is not None:
            if (
                existing.name != tool.name
                or existing.external_type != tool.external_type
            ):
                raise ResponsesConversionError(
                    f"Tool type conflict for call_id '{call_id}': "
                    f"registered as {existing.external_type} '{existing.name}', "
                    f"received {tool.external_type} '{tool.name}'"
                )
            raise ResponsesConversionError(
                f"Duplicate Tool Call call_id '{call_id}'"
            )
        self.by_call_id[call_id] = tool

    def resolve(self, call_id: str, name: str) -> ResponsesToolIR:
        """Resolve one upstream Tool Call using identity first, then name."""
        by_call_id = self.by_call_id.get(call_id)
        by_name = self.by_name.get(name)
        if by_call_id is None or by_name is None:
            raise ResponsesConversionError(
                f"Kiro returned Tool Call '{name}' with call_id '{call_id}' "
                "without a matching Client Tool registry entry"
            )
        if by_call_id is not by_name:
            raise ResponsesConversionError(
                f"Tool registry conflict for call_id '{call_id}' and name '{name}'"
            )
        return by_call_id


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
    tool_registry: ResponsesToolRegistry

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
            if not isinstance(block_type, str) or block_type not in {
                "input_text",
                "text",
                "output_text",
                "summary_text",
            }:
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
        if not isinstance(block_type, str):
            raise ResponsesConversionError(
                f"Unsupported {field_name} content type: {block_type or 'missing type'}"
            )
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

        if block_type in {"input_file", "file", "file_id", "file_url"}:
            raise ResponsesConversionError(
                f"{field_name} file references are not supported; send an inline base64 image data URL"
            )

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
            if (
                isinstance(media_type, str)
                and media_type.startswith("image/")
                and isinstance(data, str)
                and data
            ):
                return {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
        if isinstance(source, dict) and source.get("type") in {"url", "file", "file_id"}:
            raise ResponsesConversionError(
                f"{field_name} remote URLs and file references are not supported for images"
            )
        raise ResponsesConversionError(
            f"{field_name} only supports base64 image sources"
        )

    if "file_id" in block or "file_url" in block:
        raise ResponsesConversionError(
            f"{field_name} file ID and file URL references are not supported"
        )
    image_value = block.get("image_url")
    if isinstance(image_value, dict):
        if "file_id" in image_value or "file_url" in image_value:
            raise ResponsesConversionError(
                f"{field_name} file ID and file URL references are not supported"
            )
        image_url = image_value.get("url")
    else:
        image_url = image_value
    if not isinstance(image_url, str):
        raise ResponsesConversionError(
            f"{field_name} requires a base64 image data URL; remote URLs and file references are not supported"
        )
    if image_url.lower().startswith(("http://", "https://", "file://")):
        raise ResponsesConversionError(
            f"{field_name} remote URLs and file URLs are not supported; use an inline base64 image data URL"
        )
    if not image_url.startswith("data:"):
        raise ResponsesConversionError(
            f"{field_name} only supports base64 data URL images"
        )
    try:
        header, data = image_url.split(",", 1)
    except ValueError as exc:
        raise ResponsesConversionError(
            f"{field_name} requires a complete base64 image data URL"
        ) from exc
    metadata = header[5:].split(";")
    media_type = metadata[0].lower()
    if not media_type.startswith("image/") or "base64" not in {
        value.lower() for value in metadata[1:]
    } or not data:
        raise ResponsesConversionError(
            f"{field_name} requires a non-empty base64 image data URL"
        )
    return {"type": "image_url", "image_url": {"url": image_url}}


def _parse_tool_arguments(value: Any, call_id: str, tool_type: str) -> str:
    """Normalize one replayed non-custom Client Tool payload to JSON."""
    arguments = "{}" if value is None else value
    if not isinstance(arguments, str):
        try:
            arguments = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ResponsesConversionError(
                f"{tool_type}_call '{call_id}' arguments must be JSON"
            ) from exc
    try:
        parsed_arguments = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ResponsesConversionError(
            f"{tool_type}_call '{call_id}' arguments must be valid JSON"
        ) from exc
    if not isinstance(parsed_arguments, dict):
        raise ResponsesConversionError(
            f"{tool_type}_call '{call_id}' arguments must be a JSON object"
        )
    return arguments


def _tool_call_name(item: ResponsesInputItem, tool_type: str) -> str:
    """Return the explicit or protocol-defined name for a Tool Call."""
    name = item.name or _DEFAULT_TOOL_NAMES.get(tool_type)
    if not isinstance(name, str) or not name:
        raise ResponsesConversionError(
            f"{item.type} items require a non-empty tool name"
        )
    return name


def _convert_tool_call(
    item: ResponsesInputItem,
    tool_type: str,
) -> Dict[str, Any]:
    """Convert one replayed Client Tool Call to Kiro function form."""
    if not item.call_id:
        raise ResponsesConversionError(
            f"{item.type} items require call_id"
        )
    name = _tool_call_name(item, tool_type)
    if tool_type == "custom":
        if not isinstance(item.input, str):
            raise ResponsesConversionError(
                f"custom_tool_call '{item.call_id}' input must be a raw string"
            )
        arguments = json.dumps({"input": item.input}, ensure_ascii=False)
    elif tool_type == "shell":
        arguments = _parse_tool_arguments(item.action, item.call_id, tool_type)
    elif tool_type == "local_shell":
        arguments = _parse_tool_arguments(item.action, item.call_id, tool_type)
    elif tool_type == "tool_search":
        arguments = _parse_tool_arguments(item.arguments, item.call_id, tool_type)
    elif tool_type == "apply_patch":
        arguments = _parse_tool_arguments(item.operation, item.call_id, tool_type)
    else:
        arguments = _parse_tool_arguments(item.arguments, item.call_id, tool_type)
    return {
        "id": item.call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _result_call_id(item: ResponsesInputItem, tool_type: str) -> Optional[str]:
    """Return the original call identity for a replayed Tool Result."""
    if item.call_id:
        return item.call_id
    # The official local-shell output shape uses ``id`` for the originating
    # local shell call, while the other supported result shapes use call_id.
    if tool_type == "local_shell" and item.id:
        return item.id
    return None


def _convert_tool_result(
    item: ResponsesInputItem,
    tool_type: str,
) -> Dict[str, Any]:
    """Convert a replayed Responses Tool Result to unified form."""
    call_id = _result_call_id(item, tool_type)
    if not call_id:
        raise ResponsesConversionError(f"{item.type} items require call_id")
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
        "tool_use_id": call_id,
        "content": output_text or "(empty result)",
    }


def _convert_input_item(item: ResponsesInputItem) -> ResponsesInputItemIR:
    """Convert one Pydantic item to the protocol IR."""
    item_type = item.type or "message"

    if item_type in _CLIENT_CALL_ITEM_TYPES:
        tool_type = _CLIENT_CALL_ITEM_TYPES[item_type]
        call = _convert_tool_call(item, tool_type)
        return ResponsesInputItemIR(
            item_type=item_type,
            role="assistant",
            item_id=item.id,
            call_id=item.call_id,
            content=item.content,
            text="",
            tool_calls=[call],
            tool_type=tool_type,
        )

    if item_type in _CLIENT_RESULT_ITEM_TYPES:
        tool_type = _CLIENT_RESULT_ITEM_TYPES[item_type]
        result = _convert_tool_result(item, tool_type)
        return ResponsesInputItemIR(
            item_type=item_type,
            role="user",
            item_id=item.id,
            call_id=result["tool_use_id"],
            content=item.output if item.output is not None else item.content,
            text=result["content"],
            tool_result=result["content"],
            tool_type=tool_type,
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
        elif item_data.get("encrypted_content") is not None or item_data.get("encrypted_text") is not None:
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
    """Convert supported Client Tools and reject Hosted Tools explicitly."""
    converted: List[ResponsesToolIR] = []
    for tool in tools or []:
        tool_type = tool.get("type")
        if not isinstance(tool_type, str):
            supported = ", ".join(_SUPPORTED_CLIENT_TOOL_TYPES)
            raise ResponsesConversionError(
                f"Unsupported Client Tool type '{tool_type or 'missing type'}'. "
                f"Supported Client Tool types: {supported}"
            )
        if tool_type in _HOSTED_TOOL_TYPES:
            raise ResponsesConversionError(
                f"Hosted Tool '{tool_type}' is not supported by KiroaaS Responses"
            )
        if tool_type == "tool_search" and tool.get("execution") == "server":
            raise ResponsesConversionError(
                "Hosted Tool 'tool_search' with execution=server is not supported; "
                "use a client-executed tool_search definition"
            )
        if tool_type == "tool_search" and tool.get("execution") not in {
            None,
            "client",
        }:
            raise ResponsesConversionError(
                "tool_search execution must be 'client' when provided"
            )
        if tool_type not in _SUPPORTED_CLIENT_TOOL_TYPES:
            supported = ", ".join(_SUPPORTED_CLIENT_TOOL_TYPES)
            raise ResponsesConversionError(
                f"Unsupported Client Tool type '{tool_type or 'missing type'}'. "
                f"Supported Client Tool types: {supported}"
            )

        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = function.get("name") or _DEFAULT_TOOL_NAMES.get(tool_type)
        if not isinstance(name, str) or not name:
            raise ResponsesConversionError(
                f"{tool_type} tools require a non-empty name"
            )
        if tool_type == "custom":
            parameters = _CUSTOM_TOOL_PARAMETERS
        else:
            parameters = function.get(
                "parameters",
                function.get("input_schema", tool.get("parameters", {})),
            )
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise ResponsesConversionError(f"Tool '{name}' parameters must be an object")
        if tool.get("strict") is True or function.get("strict") is True:
            raise ResponsesConversionError(
                f"Tool '{name}' strict schema guarantees are not supported"
            )
        converted.append(
            ResponsesToolIR(
                external_type=tool_type,
                name=name,
                description=function.get("description"),
                parameters=parameters,
                execution=tool.get("execution") if tool_type == "tool_search" else None,
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
        return ThinkingConfig(
            enabled=False,
            enforce_budget_cap=False,
            include_system_guidance=False,
        )
    if not isinstance(effort, str) or effort not in THINKING_BUDGETS:
        allowed = ", ".join(["none", *THINKING_BUDGETS.keys()])
        raise ResponsesConversionError(
            f"Unsupported reasoning effort '{effort}'. Allowed values: {allowed}"
        )
    return ThinkingConfig(
        enabled=True,
        budget_tokens=THINKING_BUDGETS[effort],
        enforce_budget_cap=False,
    )


def _extract_verbosity(request: ResponsesRequest) -> Optional[str]:
    """Return the validated best-effort response verbosity preference."""
    if not request.text:
        return None
    verbosity = request.text.get("verbosity")
    if verbosity is None:
        return None
    if not isinstance(verbosity, str) or verbosity not in _VERBOSITY_VALUES:
        allowed = ", ".join(sorted(_VERBOSITY_VALUES))
        raise ResponsesConversionError(
            f"Unsupported text verbosity '{verbosity}'. Allowed values: {allowed}"
        )
    return verbosity


def _validate_request_capabilities(request: ResponsesRequest) -> None:
    """Reject stateful or contract-changing capabilities not implemented yet."""
    if request.stream:
        raise ResponsesConversionError(
            "Streaming Responses is not implemented; set stream=false"
        )
    if request.store is True:
        raise ResponsesConversionError("Responses storage is not supported; omit store or set store=false")
    if request.previous_response_id is not None:
        raise ResponsesConversionError("previous_response_id is not supported; resend the full input")
    if request.conversation is not None:
        raise ResponsesConversionError("Stateful conversations are not supported")
    if request.prompt is not None:
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
        _extract_verbosity(request)
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
        unsupported_text_fields = set(request.text) - {"format", "verbosity"}
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

    response_tools = _convert_tools(request.tools)
    tool_registry = ResponsesToolRegistry()
    for response_tool in response_tools:
        tool_registry.register(response_tool)
    replayed_tool_items = {
        item.item_type
        for item in items
        if item.item_type in {
            *_CLIENT_CALL_ITEM_TYPES,
            *_CLIENT_RESULT_ITEM_TYPES,
        }
    }
    if replayed_tool_items and not response_tools:
        raise ResponsesConversionError(
            "Tool Call replay requires the corresponding function definitions "
            "or Client Tool definitions in the tools field; resend the complete "
            "Client Tool registry"
        )
    known_call_ids = set()
    returned_call_ids = set()
    for item in items:
        if item.item_type in _CLIENT_CALL_ITEM_TYPES and item.call_id:
            call_name = item.tool_calls[0]["function"]["name"]
            if item.call_id in known_call_ids:
                existing_tool = tool_registry.by_call_id[item.call_id]
                expected_type = _CLIENT_CALL_ITEM_TYPES[item.item_type]
                if (
                    existing_tool.name != call_name
                    or existing_tool.external_type != expected_type
                ):
                    raise ResponsesConversionError(
                        f"Tool type conflict for call_id '{item.call_id}': "
                        f"registered as {existing_tool.external_type} "
                        f"'{existing_tool.name}', received {expected_type} "
                        f"'{call_name}'"
                    )
                raise ResponsesConversionError(
                    f"Duplicate {item.item_type} call_id: {item.call_id}"
                )
            registered_tool = tool_registry.by_name.get(call_name)
            if registered_tool is None:
                raise ResponsesConversionError(
                    f"function_call '{item.call_id}' references unregistered tool "
                    f"'{call_name}'; resend its function definition in tools"
                )
            expected_type = _CLIENT_CALL_ITEM_TYPES[item.item_type]
            if registered_tool.external_type != expected_type:
                raise ResponsesConversionError(
                    f"Tool type conflict for call_id '{item.call_id}': "
                    f"item is {expected_type}, registry entry is "
                    f"{registered_tool.external_type}"
                )
            tool_registry.bind_call(item.call_id, registered_tool)
            known_call_ids.add(item.call_id)
        elif item.item_type in _CLIENT_RESULT_ITEM_TYPES:
            if item.call_id not in known_call_ids:
                expected_type = _CLIENT_RESULT_ITEM_TYPES[item.item_type]
                preceding = (
                    "function_call"
                    if expected_type == "function"
                    else "Tool Call"
                )
                raise ResponsesConversionError(
                    f"{item.item_type} has no preceding {preceding} for call_id "
                    f"{item.call_id}"
                )
            registered_tool = tool_registry.by_call_id[item.call_id]
            expected_type = _CLIENT_RESULT_ITEM_TYPES[item.item_type]
            if registered_tool.external_type != expected_type:
                raise ResponsesConversionError(
                    f"Tool type conflict for call_id '{item.call_id}': "
                    f"result is {expected_type}, registry entry is "
                    f"{registered_tool.external_type}"
                )
            if item.call_id in returned_call_ids:
                raise ResponsesConversionError(
                    f"Duplicate {item.item_type} for call_id "
                    f"{item.call_id}; send exactly one result for each Tool Call"
                )
            returned_call_ids.add(item.call_id)
    missing_results = known_call_ids - returned_call_ids
    if missing_results:
        missing = ", ".join(sorted(missing_results))
        missing_label = (
            "missing matching function_call_output items"
            if all(
                tool_registry.by_call_id[call_id].external_type == "function"
                for call_id in missing_results
            )
            else "missing matching Tool Result items"
        )
        raise ResponsesConversionError(
            f"Tool Call items have {missing_label}: "
            f"{missing}"
        )

    instruction_segments: List[str] = []
    instructions_text = _extract_instruction_text(request.instructions, "instructions")
    if instructions_text:
        instruction_segments.append(instructions_text)
    for item in items:
        if item.role in {"system", "developer"} and item.text:
            instruction_segments.append(item.text)
    verbosity = _extract_verbosity(request)
    if verbosity:
        instruction_segments.append(
            f"Response verbosity preference: {verbosity}. Treat this as a best-effort style preference."
        )
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
                if item.item_type in _CLIENT_RESULT_ITEM_TYPES
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
        tool_registry=tool_registry,
    )
