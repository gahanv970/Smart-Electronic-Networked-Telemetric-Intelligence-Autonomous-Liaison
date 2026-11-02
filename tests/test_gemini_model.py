"""Offline tests for the Gemini function-calling adapter.

``google-genai`` itself is real (for its Pydantic types), but the client is a
local fake and no HTTP request is made.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.agent_loop import (
    AgentCancelled,
    AgentMessage,
    CancellationToken,
    ToolCall,
    ToolResponse,
)
from engine.gemini_model import (
    GeminiModelError,
    GeminiToolChatModel,
    ModelConfigurationError,
    build_contents,
    json_schema_to_gemini,
)
from engine.tool_registry import Permission, ToolRegistry


# ---------------------------------------------------------------------------
# Fake client / response helpers
# ---------------------------------------------------------------------------


class FakeModels:
    def __init__(self, response=None, error=None, on_call=None):
        self.response = response
        self.error = error
        self.on_call = on_call
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None, on_call=None):
        self.models = FakeModels(response=response, error=error, on_call=on_call)


def response_with(parts, finish="STOP"):
    from google.genai import types

    finish_reason = getattr(types.FinishReason, finish, None)
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)),
                finish_reason=finish_reason,
            )
        ]
    )


def text_part(text):
    from google.genai import types

    return types.Part.from_text(text=text)


def call_part(name, arguments, call_id=None, thought_signature=None):
    from google.genai import types

    kwargs = {"name": name, "args": arguments}
    if call_id:
        kwargs["id"] = call_id
    part_kwargs = {"function_call": types.FunctionCall(**kwargs)}
    if thought_signature is not None:
        part_kwargs["thought_signature"] = thought_signature
    return types.Part(**part_kwargs)


def make_registry():
    registry = ToolRegistry("gemini-test")

    def echo_text(text: str):
        return {"success": True, "data": {"text": text}}

    registry.register(
        "echo_text",
        "Echo text",
        {
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
        },
        echo_text,
    )
    registry.register(
        "delete_item",
        "Delete one item",
        {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
        lambda **kwargs: {"success": True},
        permission=Permission.CONFIRMATION_REQUIRED,
    )
    return registry


def make_model(response=None, **client_kwargs):
    client = FakeClient(response=response, **client_kwargs)
    return GeminiToolChatModel(client, "gemini-2.0-flash"), client


# ---------------------------------------------------------------------------
# Schema conversion and request configuration
# ---------------------------------------------------------------------------


def test_json_schema_conversion_and_request_configuration():
    from google.genai import types

    registry = ToolRegistry("schema")
    registry.register(
        "nested_tool",
        "Nested tool",
        {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["fast", "safe"]},
                "count": {"type": "integer", "minimum": 0, "maximum": 10},
                "metadata": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "minLength": 1}},
                    "required": ["label"],
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                },
            },
            "required": ["mode"],
        },
        lambda **kwargs: {"success": True},
    )

    model, client = make_model(response_with([text_part("ok")]))
    result = model.generate(
        [AgentMessage(role="user", text="hi")],
        registry.specs(),
        system_instruction="system prompt",
        timeout_seconds=1.5,
    )

    assert result.text == "ok"
    assert client.models.calls

    config = client.models.calls[0]["config"]
    assert config.automatic_function_calling.disable is True
    assert config.http_options.timeout == 1500
    assert config.http_options.retry_options.attempts == 1
    assert config.system_instruction == "system prompt"
    assert config.temperature == 0.2

    declaration = config.tools[0].function_declarations[0]
    assert declaration.name == "nested_tool"
    schema = declaration.parameters
    assert schema.type == types.Type.OBJECT
    # Gemini rejects additional_properties on the wire even though the SDK
    # exposes it.  The registry enforces the same restriction locally.
    assert schema.additional_properties is None
    serialized = declaration.model_dump_json(exclude_none=True)
    assert "additional_properties" not in serialized
    assert schema.required == ["mode"]
    assert schema.properties["mode"].enum == ["fast", "safe"]
    assert schema.properties["count"].type == types.Type.INTEGER
    assert schema.properties["count"].minimum == 0
    assert schema.properties["count"].maximum == 10
    assert schema.properties["metadata"].type == types.Type.OBJECT
    assert schema.properties["metadata"].required == ["label"]
    assert schema.properties["tags"].items.type == types.Type.STRING
    assert schema.properties["tags"].max_items == 3


def _assert_no_additional_properties(payload):
    """Recursively assert the live-API-incompatible keyword is absent."""
    if isinstance(payload, dict):
        assert "additional_properties" not in payload
        assert "additionalProperties" not in payload
        for value in payload.values():
            _assert_no_additional_properties(value)
    elif isinstance(payload, list):
        for value in payload:
            _assert_no_additional_properties(value)


def test_every_registered_app_tool_serializes_for_gemini():
    """Every production declaration must be accepted by Gemini's schema parser.

    This is an offline construction/serialization test; it does not claim the
    live Gemini API has accepted the request.
    """
    from engine.app_tools import APP_TOOL_NAMES, build_app_registry

    class Stub:
        def create_task(self, **kwargs):
            return None

        def get_task(self, task_id):
            return None

        def get_all_tasks(self, **kwargs):
            return []

        def get_all_memories(self, **kwargs):
            return []

        def get_system_status(self):
            return None

        def navigate_url(self, url, user_confirmed=False):
            return None

    stub = Stub()
    registry = build_app_registry(
        task_service=stub,
        memory_service=stub,
        automation_service=stub,
        browser_service=stub,
    )
    model, client = make_model(response=response_with([text_part("ok")]))
    model.generate(
        [AgentMessage(role="user", text="hi")],
        registry.specs(),
        system_instruction="sys",
        timeout_seconds=5,
    )

    declarations = client.models.calls[0]["config"].tools[0].function_declarations
    assert tuple(item.name for item in declarations) == APP_TOOL_NAMES
    for declaration in declarations:
        payload = declaration.model_dump(mode="json", exclude_none=True)
        _assert_no_additional_properties(payload)
        assert payload["parameters"]["type"] == "OBJECT"
        assert payload["name"] == declaration.name
        # Every required field must still be advertised.
        for required_name in payload["parameters"].get("required", []):
            assert required_name in payload["parameters"]["properties"]

    create_schema = next(
        item for item in declarations if item.name == "create_task"
    ).parameters
    assert create_schema.properties["priority"].enum == [
        "LOW",
        "MEDIUM",
        "HIGH",
        "CRITICAL",
    ]
    assert create_schema.required == ["title"]
    assert create_schema.properties["title"].min_length == 1
    assert create_schema.properties["title"].max_length == 200


def test_open_object_schema_is_rejected_for_gemini():
    with pytest.raises(ModelConfigurationError):
        json_schema_to_gemini(
            {"type": "object", "additionalProperties": True, "properties": {}}
        )


def test_multi_type_schema_becomes_any_of():
    from google.genai import types

    schema = json_schema_to_gemini(
        {"type": ["string", "integer"], "description": "identifier"}
    )

    assert schema.type is None
    assert [item.type for item in schema.any_of] == [
        types.Type.STRING,
        types.Type.INTEGER,
    ]


def test_const_schema_becomes_a_single_value_enum():
    schema = json_schema_to_gemini({"type": "string", "const": "fixed"})

    assert schema.enum == ["fixed"]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"x": {"$ref": "#/x"}}},
        {"type": "object", "properties": {"x": {"type": "number", "exclusiveMinimum": 1}}},
        {"type": "string", "enum": [1, 2]},
        {"type": "string", "const": 5},
        {"type": "object", "additionalProperties": {"type": "string"}},
    ],
)
def test_unrepresentable_schemas_are_rejected(schema):
    with pytest.raises(ModelConfigurationError):
        json_schema_to_gemini(schema)


def test_non_object_schema_rejected():
    with pytest.raises(ModelConfigurationError):
        json_schema_to_gemini(["not", "an", "object"])


def test_no_tools_sends_no_tool_declaration():
    model, client = make_model(response_with([text_part("hello")]))

    model.generate(
        [AgentMessage(role="user", text="hi")],
        [],
        system_instruction="sys",
        timeout_seconds=5,
    )

    assert client.models.calls[0]["config"].tools is None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_response_text_and_function_calls_are_parsed():
    signature = b"opaque-thought-signature"
    response = response_with(
        [
            text_part("Checking. "),
            call_part(
                "echo_text",
                {"text": "hi"},
                call_id="call-1",
                thought_signature=signature,
            ),
            text_part("Done."),
        ]
    )
    model, _client = make_model(response=response)

    result = model.generate(
        [AgentMessage(role="user", text="hi")],
        make_registry().specs(),
        system_instruction="sys",
        timeout_seconds=5,
    )

    assert result.text == "Checking. Done."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "echo_text"
    assert result.tool_calls[0].arguments == {"text": "hi"}
    assert result.tool_calls[0].call_id == "call-1"
    assert result.tool_calls[0].thought_signature == signature
    assert result.finish_reason == "STOP"


def test_function_call_with_missing_arguments_becomes_an_empty_object():
    response = response_with([call_part("delete_item", None)])
    model, _client = make_model(response=response)

    result = model.generate(
        [AgentMessage(role="user", text="hi")],
        make_registry().specs(),
        system_instruction="sys",
        timeout_seconds=5,
    )

    assert result.tool_calls[0].arguments == {}


def test_function_call_with_non_object_arguments_is_rejected():
    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            function_call=SimpleNamespace(
                                name="echo_text",
                                args=["not", "an", "object"],
                                id=None,
                            )
                        )
                    ]
                ),
                finish_reason=None,
            )
        ]
    )
    model, _client = make_model(response=response)

    with pytest.raises(GeminiModelError):
        model.generate(
            [AgentMessage(role="user", text="hi")],
            make_registry().specs(),
            system_instruction="sys",
            timeout_seconds=5,
        )


def test_empty_response_is_returned_without_inventing_text():
    model, _client = make_model(response=SimpleNamespace(candidates=[]))

    result = model.generate(
        [AgentMessage(role="user", text="hi")],
        [],
        system_instruction="sys",
        timeout_seconds=5,
    )

    assert result.text == ""
    assert result.tool_calls == ()


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def test_contents_preserve_tool_call_and_function_response_ids():
    messages = [
        AgentMessage(role="user", text="please echo"),
        AgentMessage(
            role="model",
            text="Working",
            tool_calls=(
                ToolCall(
                    "echo_text",
                    {"text": "hi"},
                    call_id="call-9",
                    thought_signature=b"signature",
                ),
            ),
        ),
        AgentMessage(
            role="tool",
            tool_responses=(
                ToolResponse(
                    "echo_text",
                    {"success": True, "message": "ok"},
                    call_id="call-9",
                ),
            ),
        ),
    ]

    contents = build_contents(messages)

    assert [content.role for content in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].text == "Working"
    assert contents[1].parts[1].function_call.name == "echo_text"
    assert contents[1].parts[1].function_call.id == "call-9"
    assert contents[1].parts[1].thought_signature == b"signature"
    assert contents[2].parts[0].function_response.name == "echo_text"
    assert contents[2].parts[0].function_response.id == "call-9"
    assert contents[2].parts[0].function_response.response["success"] is True


def test_consecutive_tool_responses_are_grouped_in_one_user_turn():
    messages = [
        AgentMessage(role="user", text="two tools"),
        AgentMessage(
            role="model",
            tool_calls=(
                ToolCall("first_tool", {}, call_id="1"),
                ToolCall("second_tool", {}, call_id="2"),
            ),
        ),
        AgentMessage(
            role="tool",
            tool_responses=(ToolResponse("first_tool", {"success": True}, "1"),),
        ),
        AgentMessage(
            role="tool",
            tool_responses=(ToolResponse("second_tool", {"success": False}, "2"),),
        ),
    ]

    contents = build_contents(messages)

    assert [content.role for content in contents] == ["user", "model", "user"]
    assert len(contents[2].parts) == 2
    assert contents[2].parts[0].function_response.id == "1"
    assert contents[2].parts[1].function_response.id == "2"


# ---------------------------------------------------------------------------
# Cancellation and errors
# ---------------------------------------------------------------------------


def test_already_cancelled_token_prevents_the_request():
    token = CancellationToken()
    token.cancel("stop")
    model, client = make_model(response_with([text_part("never")]))

    with pytest.raises(AgentCancelled):
        model.generate(
            [AgentMessage(role="user", text="hi")],
            [],
            system_instruction="sys",
            timeout_seconds=5,
            cancellation=token,
        )

    assert client.models.calls == []


def test_cancellation_after_the_request_discards_the_response():
    token = CancellationToken()

    def cancel_during_call():
        token.cancel("user cancelled")

    model, _client = make_model(
        response=response_with([text_part("late")]),
        on_call=cancel_during_call,
    )

    with pytest.raises(AgentCancelled):
        model.generate(
            [AgentMessage(role="user", text="hi")],
            [],
            system_instruction="sys",
            timeout_seconds=5,
            cancellation=token,
        )


def test_transport_error_is_wrapped_and_redacted():
    model, _client = make_model(error=RuntimeError("api_key=SUPERSECRETVALUE"))

    with pytest.raises(GeminiModelError) as captured:
        model.generate(
            [AgentMessage(role="user", text="hi")],
            [],
            system_instruction="sys",
            timeout_seconds=5,
        )

    message = str(captured.value)
    assert "SUPERSECRETVALUE" not in message
    assert "[REDACTED]" in message


def test_invalid_timeout_is_rejected_before_the_request():
    model, client = make_model(response_with([text_part("ok")]))

    with pytest.raises(ModelConfigurationError):
        model.generate(
            [AgentMessage(role="user", text="hi")],
            [],
            system_instruction="sys",
            timeout_seconds=0,
        )

    assert client.models.calls == []


@pytest.mark.parametrize(
    "client,name",
    [
        (None, "gemini-2.0-flash"),
        (FakeClient(response=SimpleNamespace(candidates=[])), ""),
        (FakeClient(response=SimpleNamespace(candidates=[])), "   "),
    ],
)
def test_model_configuration_validation(client, name):
    with pytest.raises(ModelConfigurationError):
        GeminiToolChatModel(client, name)


def test_temperature_validation():
    client = FakeClient(response=SimpleNamespace(candidates=[]))
    with pytest.raises(ModelConfigurationError):
        GeminiToolChatModel(client, "gemini-2.0-flash", temperature=3)
    with pytest.raises(ModelConfigurationError):
        GeminiToolChatModel(client, "gemini-2.0-flash", temperature=True)
