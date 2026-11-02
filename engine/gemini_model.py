"""Google Gen AI (``google-genai``) transport for the S.E.N.T.I.A.L. agent loop.

Only transport and schema translation live here.  Tool authorization, limits,
sequencing and confirmation stay in :mod:`engine.agent_loop` and
:mod:`engine.tool_registry`.

Key properties
--------------
* Automatic function calling is **disabled**.  The SDK must never execute a
  tool by itself; every call goes back through the registry and the app-side
  confirmation flow.
* The registry's JSON Schema is converted strictly.  A keyword the Gemini
  ``Schema`` type cannot express is rejected instead of being silently dropped,
  because dropping it would weaken argument validation.  ``additionalProperties``
  is the one deliberate exception: the registry already enforces
  ``additionalProperties: false`` during local validation, and the live Gemini
  API rejects that keyword in function declarations even though the SDK models
  it.  It is validated here and then omitted from the wire schema.
* HTTP timeouts are set from the remaining workflow budget and SDK retries are
  limited to one attempt, so the loop never silently extends a limit.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

from engine.agent_loop import (
    AgentCancelled,
    AgentMessage,
    CancellationToken,
    ModelResponse,
    ToolCall,
)
from engine.tool_registry import ToolSpec

logger = logging.getLogger(__name__)

__all__ = [
    "GeminiModelError",
    "GeminiToolChatModel",
    "ModelConfigurationError",
    "json_schema_to_gemini",
]


class GeminiModelError(Exception):
    """Raised when the Gemini request or response cannot be used safely."""


class ModelConfigurationError(GeminiModelError):
    """Raised when a registry schema cannot be represented by Gemini."""


def _types():
    """Import ``google.genai.types`` lazily so importing engine has no cost."""
    try:
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise GeminiModelError(
            "google-genai is not installed; the Gemini agent model is unavailable."
        ) from exc
    return types


def _redact(text: Any, limit: int = 500) -> str:
    import re as _re

    cleaned = str(text)
    patterns = (
        (
            _re.compile(
                r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|authorization)"
                r"\s*[:=]\s*\S+"
            ),
            r"\1=[REDACTED]",
        ),
        (_re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"), "[REDACTED]"),
        (_re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
    )
    for pattern, replacement in patterns:
        cleaned = pattern.sub(replacement, cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3] + "..."
    return cleaned


# ---------------------------------------------------------------------------
# JSON Schema -> google.genai.types.Schema
# ---------------------------------------------------------------------------

_TYPE_NAMES = {
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
    "null": "NULL",
}

_SCHEMA_SUPPORTED = frozenset(
    {
        "additionalProperties",
        "const",
        "description",
        "enum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)


def _schema_type(name: str) -> Any:
    types = _types()
    try:
        return getattr(types.Type, _TYPE_NAMES[name])
    except KeyError as exc:
        raise ModelConfigurationError(
            f"Gemini cannot represent JSON-schema type '{name}'."
        ) from exc


def json_schema_to_gemini(schema: Mapping[str, Any], path: str = "parameters") -> Any:
    """Convert a registry schema object into ``types.Schema``.

    Raises :class:`ModelConfigurationError` for keywords Gemini cannot express.
    """
    types = _types()

    if not isinstance(schema, Mapping):
        raise ModelConfigurationError(f"{path} must be a JSON-schema object.")

    unknown = set(schema) - _SCHEMA_SUPPORTED
    if unknown:
        raise ModelConfigurationError(
            f"{path} uses schema keyword(s) Gemini cannot express: "
            f"{', '.join(sorted(unknown))}. Refusing to weaken validation."
        )

    kwargs: Dict[str, Any] = {}

    if "type" in schema:
        declared = schema["type"]
        names = declared if isinstance(declared, (list, tuple)) else [declared]
        if not names or not all(isinstance(item, str) for item in names):
            raise ModelConfigurationError(f"{path}.type must be string or string list.")
        converted = [_schema_type(item) for item in names]
        if len(converted) == 1:
            kwargs["type"] = converted[0]
        else:
            kwargs["any_of"] = [types.Schema(type=item) for item in converted]

    for keyword, field in (
        ("title", "title"),
        ("description", "description"),
        ("pattern", "pattern"),
    ):
        if keyword in schema:
            if not isinstance(schema[keyword], str):
                raise ModelConfigurationError(f"{path}.{keyword} must be a string.")
            kwargs[field] = schema[keyword]

    for keyword, field in (
        ("minLength", "min_length"),
        ("maxLength", "max_length"),
        ("minItems", "min_items"),
        ("maxItems", "max_items"),
        ("minimum", "minimum"),
        ("maximum", "maximum"),
    ):
        if keyword in schema:
            kwargs[field] = schema[keyword]

    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, (list, tuple)) or not values:
            raise ModelConfigurationError(f"{path}.enum must be a non-empty list.")
        if not all(isinstance(item, str) for item in values):
            raise ModelConfigurationError(
                f"{path}.enum contains non-string values, which Gemini's "
                "function schema cannot represent faithfully."
            )
        kwargs["enum"] = list(values)

    if "const" in schema:
        if not isinstance(schema["const"], str):
            raise ModelConfigurationError(
                f"{path}.const is not a string; Gemini's function schema cannot "
                "represent it faithfully."
            )
        # Gemini Schema has no ``const``; a one-value enum is equivalent.
        kwargs["enum"] = [schema["const"]]

    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, (list, tuple)):
            raise ModelConfigurationError(f"{path}.required must be a list.")
        kwargs["required"] = [str(item) for item in required]

    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, Mapping):
            raise ModelConfigurationError(f"{path}.properties must be an object.")
        kwargs["properties"] = {
            str(name): json_schema_to_gemini(sub, f"{path}.properties.{name}")
            for name, sub in properties.items()
        }

    if "items" in schema:
        kwargs["items"] = json_schema_to_gemini(schema["items"], f"{path}.items")

    if "additionalProperties" in schema:
        additional = schema["additionalProperties"]
        if additional is not False:
            # Open objects cannot be described faithfully to the model.  The
            # registry always requires ``false``; reject anything else instead
            # of pretending the declaration is equivalent.
            raise ModelConfigurationError(
                f"{path}.additionalProperties must be false; Gemini function "
                "declarations cannot represent open objects."
            )
        # Deliberately not forwarded: google-genai serializes this as
        # ``additional_properties`` and Gemini's API rejects the unknown field
        # (HTTP 400).  Local registry validation still enforces it.

    try:
        return types.Schema(**kwargs)
    except Exception as exc:  # noqa: BLE001 - SDK validation boundary
        raise ModelConfigurationError(
            f"{path} could not be converted for Gemini: {_redact(exc)}"
        ) from exc


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def _function_call_part(call: ToolCall) -> Any:
    types = _types()
    function_call = types.FunctionCall(
        name=call.name,
        args=dict(call.arguments),
        **({"id": call.call_id} if call.call_id else {}),
    )
    if call.thought_signature is not None:
        return types.Part(
            function_call=function_call,
            thought_signature=call.thought_signature,
        )
    return types.Part(function_call=function_call)


def _function_response_part(response: Any) -> Any:
    types = _types()
    payload = dict(response.result)
    kwargs: Dict[str, Any] = {"name": response.name, "response": payload}
    if response.call_id:
        kwargs["id"] = response.call_id
    return types.Part(function_response=types.FunctionResponse(**kwargs))


def build_contents(messages: Sequence[AgentMessage]) -> List[Any]:
    """Convert provider-neutral messages into Gemini ``Content`` objects."""
    types = _types()
    contents: List[Any] = []
    previous_was_tool = False

    for message in messages:
        if message.role == "user":
            parts = [types.Part.from_text(text=message.text)] if message.text else []
            contents.append(types.Content(role="user", parts=parts))
            previous_was_tool = False
            continue

        if message.role == "model":
            parts: List[Any] = []
            if message.text:
                parts.append(types.Part.from_text(text=message.text))
            parts.extend(_function_call_part(call) for call in message.tool_calls)
            contents.append(types.Content(role="model", parts=parts))
            previous_was_tool = False
            continue

        # Function responses are a user-role turn in the Gemini API.
        response_parts = [
            _function_response_part(response) for response in message.tool_responses
        ]
        if previous_was_tool and contents and contents[-1].role == "user":
            contents[-1].parts.extend(response_parts)
        else:
            contents.append(types.Content(role="user", parts=response_parts))
        previous_was_tool = True

    return contents


def _parse_response(response: Any) -> ModelResponse:
    """Extract text and function calls from a ``GenerateContentResponse``."""
    texts: List[str] = []
    calls: List[ToolCall] = []
    finish_reason = ""

    candidates = getattr(response, "candidates", None) or []
    if candidates:
        candidate = candidates[0]
        finish = getattr(candidate, "finish_reason", None)
        if finish is not None:
            finish_reason = getattr(finish, "name", str(finish))
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            function_call = getattr(part, "function_call", None)
            if function_call is not None:
                name = getattr(function_call, "name", "")
                if not isinstance(name, str) or not name.strip():
                    raise GeminiModelError(
                        "Gemini returned a function call without a tool name."
                    )
                arguments = getattr(function_call, "args", None)
                if arguments is None:
                    arguments = {}
                if not isinstance(arguments, Mapping):
                    raise GeminiModelError(
                        "Gemini returned function-call arguments that are not an object."
                    )
                calls.append(
                    ToolCall(
                        name=name.strip(),
                        arguments=dict(arguments),
                        call_id=getattr(function_call, "id", None),
                        thought_signature=getattr(part, "thought_signature", None),
                    )
                )
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                texts.append(text)
    else:
        text = getattr(response, "text", None)
        if isinstance(text, str):
            texts.append(text)

    return ModelResponse(
        text="".join(texts),
        tool_calls=tuple(calls),
        finish_reason=finish_reason,
        raw=response,
    )


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class GeminiToolChatModel:
    """Adapter implementing the agent loop's ``generate(...)`` contract."""

    def __init__(
        self,
        client: Any,
        model_name: str,
        *,
        system_instruction: str = "",
        temperature: float = 0.2,
    ) -> None:
        if client is None:
            raise ModelConfigurationError("A google-genai client is required.")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ModelConfigurationError("A Gemini model name is required.")
        if not isinstance(system_instruction, str):
            raise ModelConfigurationError("system_instruction must be a string.")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ModelConfigurationError("temperature must be a number.")
        if not 0.0 <= float(temperature) <= 2.0:
            raise ModelConfigurationError("temperature must be between 0 and 2.")

        self._client = client
        self._model_name = model_name.strip()
        self._system_instruction = system_instruction.strip()
        self._temperature = float(temperature)

    @property
    def model_name(self) -> str:
        return self._model_name

    def build_tool(self, tools: Sequence[ToolSpec]) -> Optional[Any]:
        types = _types()
        if not tools:
            return None
        declarations = [
            types.FunctionDeclaration(
                name=spec.name,
                description=spec.description,
                parameters=json_schema_to_gemini(
                    spec.parameters, f"tools.{spec.name}.parameters"
                ),
            )
            for spec in tools
        ]
        return types.Tool(function_declarations=declarations)

    def generate(
        self,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolSpec],
        *,
        system_instruction: str,
        timeout_seconds: Optional[float],
        cancellation: Optional[CancellationToken] = None,
    ) -> ModelResponse:
        types = _types()

        if cancellation is not None and cancellation.is_cancelled:
            raise AgentCancelled(cancellation.reason or "Cancelled before model call.")

        contents = build_contents(messages)
        tool = self.build_tool(tools)

        instructions = (system_instruction or "").strip() or self._system_instruction

        http_options = None
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or timeout_seconds <= 0
            ):
                raise ModelConfigurationError(
                    "timeout_seconds must be a positive number or None."
                )
            # google-genai expresses HTTP timeouts in milliseconds.
            http_options = types.HttpOptions(
                timeout=max(1, int(float(timeout_seconds) * 1000)),
                retry_options=types.HttpRetryOptions(attempts=1),
            )

        config = types.GenerateContentConfig(
            system_instruction=instructions or None,
            temperature=self._temperature,
            tools=[tool] if tool is not None else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            http_options=http_options,
        )

        try:
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=contents,
                config=config,
            )
        except AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - transport boundary
            logger.error("Gemini generation failed: %s", _redact(exc))
            raise GeminiModelError(
                f"Gemini request failed: {_redact(exc)}"
            ) from exc

        if cancellation is not None and cancellation.is_cancelled:
            # The loop discards this anyway, but never hand a stale result back.
            raise AgentCancelled(cancellation.reason or "Cancelled after model call.")

        return _parse_response(response)
