"""Offline unit tests for the Phase 1 tool registry (``engine.tool_registry``).

Every test in this module is hermetic: no network, no Qt, no subprocesses except
one deliberate import-side-effect check that runs a short-lived Python child.
"""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

from engine.tool_registry import (
    ConfirmationBroker,
    ConfirmationError,
    ConfirmationGrant,
    ConfirmationRequest,
    ConfirmationSpoofError,
    Permission,
    ToolNotFoundError,
    ToolRegistrationError,
    ToolRegistry,
    ToolResult,
    ToolValidationError,
    get_default_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    return ToolRegistry("test-registry")


def simple_schema():
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 40},
            "count": {"type": "integer", "minimum": 0, "maximum": 10},
        },
        "required": ["name"],
    }


def register_echo(registry, name="echo", permission=Permission.READ_ONLY, handler=None):
    calls = []

    def default_handler(**kwargs):
        calls.append(kwargs)
        return {"success": True, "message": "ok", "data": {"echo": kwargs}}

    return registry.register(
        name=name,
        description="Test tool",
        parameters=simple_schema(),
        handler=handler or default_handler,
        permission=permission,
    ), calls


def make_confirmation_registry(approved=True, ttl_seconds=120.0):
    registry = ToolRegistry("confirm-test")
    calls = []

    def handler(**kwargs):
        calls.append(kwargs)
        return {"success": True, "message": "executed"}

    registry.register(
        name="dangerous_action",
        description="Does a consequential thing",
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string", "minLength": 1}},
            "required": ["target"],
        },
        handler=handler,
        permission=Permission.CONFIRMATION_REQUIRED,
    )
    approver = MagicMock(return_value=approved)
    broker = ConfirmationBroker(registry, approver, ttl_seconds=ttl_seconds)
    return registry, broker, approver, calls


# ---------------------------------------------------------------------------
# Registration and lookup
# ---------------------------------------------------------------------------


def test_register_and_lookup(registry):
    entry, _ = register_echo(registry)

    assert entry.name == "echo"
    assert registry.contains("echo") is True
    assert len(registry) == 1
    assert registry.names() == ("echo",)

    fetched = registry.get("echo")
    assert fetched is entry
    assert fetched.spec.description == "Test tool"
    assert fetched.spec.permission is Permission.READ_ONLY
    assert callable(fetched.handler)


def test_decorator_registration_returns_original_handler(registry):
    @registry.tool(
        "greet",
        "Say hello",
        {"type": "object", "properties": {"who": {"type": "string"}}},
    )
    def greet(who="world"):
        return f"hello {who}"

    assert registry.contains("greet")
    assert registry.get("greet").handler is greet


def test_duplicate_registration_rejected(registry):
    register_echo(registry)

    with pytest.raises(ToolRegistrationError, match="already registered"):
        register_echo(registry)

    assert len(registry) == 1


def test_same_name_allowed_in_a_different_registry():
    first = ToolRegistry("first")
    second = ToolRegistry("second")
    register_echo(first)
    register_echo(second)

    assert first.contains("echo") and second.contains("echo")
    assert first is not second


def test_unknown_tool_lookup_raises(registry):
    with pytest.raises(ToolNotFoundError, match="not registered"):
        registry.get("nope")
    assert registry.find("nope") is None
    assert registry.contains("nope") is False


def test_unknown_tool_execute_returns_structured_failure(registry):
    result = registry.execute("nope", {})

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.code == "not_found"
    assert "Unknown tool" in result.message


@pytest.mark.parametrize(
    "name",
    ["", "a", "1abc", "Has Space", "has-dash", "tool.name", "a" * 65, "TOOL"],
)
def test_invalid_tool_names_rejected(registry, name):
    with pytest.raises(ToolRegistrationError):
        registry.register(name, "desc", {}, lambda: None)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"description": ""},
        {"description": "   "},
        {"description": "ok", "handler": None},
        {"description": "ok", "permission": "SUPER_USER"},
        {"description": "ok", "permission": object()},
        {"description": "ok", "category": ""},
        {"description": "ok", "timeout_seconds": 0},
        {"description": "ok", "timeout_seconds": -3},
        {"description": "ok", "timeout_seconds": "60"},
        {"description": "ok", "parameters": ["not", "a", "schema"]},
        {"description": "ok", "parameters": {"type": "array"}},
        {"description": "ok", "parameters": {"type": "object", "additionalProperties": True}},
        {
            "description": "ok",
            "parameters": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        },
        {
            "description": "ok",
            "parameters": {"type": "object", "required": ["missing"]},
        },
        {
            "description": "ok",
            "parameters": {"type": "object", "properties": {"x": {"$ref": "#/x"}}},
        },
        {
            "description": "ok",
            "parameters": {"type": "object", "properties": {"x": {"pattern": "["}}},
        },
        {
            "description": "ok",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "object", "properties": {"y": {"type": "unsupported"}}}},
            },
        },
    ],
)
def test_invalid_tool_definitions_rejected(registry, kwargs):
    params = dict(kwargs)
    description = params.pop("description")
    handler = params.pop("handler", lambda: None)
    with pytest.raises(ToolRegistrationError):
        registry.register("valid_name", description, handler=handler, **params)


@pytest.mark.parametrize("field", ["_agent_context", "user_confirmed", "app_confirmed", "confirmed", "confirmation_token"])
def test_reserved_confirmation_fields_cannot_be_declared(registry, field):
    with pytest.raises(ToolRegistrationError, match="reserved field"):
        registry.register(
            "bad_schema",
            "desc",
            {"type": "object", "properties": {field: {"type": "boolean"}}},
            lambda **kwargs: None,
        )


def test_registry_name_must_be_non_empty():
    with pytest.raises(ValueError):
        ToolRegistry("")
    with pytest.raises(ValueError):
        ToolRegistry("   ")


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------


def test_json_schema_generation(registry):
    register_echo(registry)

    schema = registry.json_schemas()[0]

    assert schema["name"] == "echo"
    assert schema["description"] == "Test tool"
    assert schema["parameters"]["type"] == "object"
    assert schema["parameters"]["additionalProperties"] is False
    assert schema["parameters"]["required"] == ["name"]
    assert schema["parameters"]["properties"]["count"]["maximum"] == 10


def test_spec_parameters_are_read_only(registry):
    entry, _ = register_echo(registry)

    with pytest.raises(TypeError):
        entry.spec.parameters["type"] = "array"
    with pytest.raises(TypeError):
        entry.spec.parameters["properties"]["name"]["type"] = "integer"


def test_json_schemas_are_detached_copies(registry):
    register_echo(registry)

    first = registry.json_schemas()[0]
    first["parameters"]["properties"]["name"]["type"] = "integer"
    first["parameters"]["required"].append("injected")

    second = registry.json_schemas()[0]
    assert second["parameters"]["properties"]["name"]["type"] == "string"
    assert second["parameters"]["required"] == ["name"]


def test_spec_to_dict_exposes_permission(registry):
    register_echo(registry, permission=Permission.REVERSIBLE)
    payload = registry.specs()[0].to_dict()

    assert payload["permission"] == "REVERSIBLE"
    assert payload["requires_confirmation"] is False
    assert payload["category"] == "general"


# ---------------------------------------------------------------------------
# Argument validation (strict, no coercion)
# ---------------------------------------------------------------------------


def test_valid_arguments_are_returned_as_a_detached_copy(registry):
    entry, _ = register_echo(registry)

    supplied = {"name": "alpha", "count": 3}
    validated = registry.validate_arguments("echo", supplied)

    assert validated == supplied
    validated["name"] = "mutated"
    assert supplied["name"] == "alpha"


def test_missing_required_argument_rejected(registry):
    register_echo(registry)
    with pytest.raises(ToolValidationError, match="required field is missing"):
        registry.validate_arguments("echo", {})


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": 123},
        {"name": None},
        {"name": "ok", "count": "3"},
        {"name": "ok", "count": True},
        {"name": "ok", "count": 1.5},
        {"name": "ok", "count": 11},
        {"name": "ok", "count": -1},
        {"name": ""},
        {"name": "x" * 41},
    ],
)
def test_invalid_argument_values_rejected(registry, arguments):
    register_echo(registry)
    with pytest.raises(ToolValidationError):
        registry.validate_arguments("echo", arguments)


def test_unexpected_argument_rejected(registry):
    register_echo(registry)
    with pytest.raises(ToolValidationError, match="unexpected field"):
        registry.validate_arguments("echo", {"name": "ok", "surprise": 1})


@pytest.mark.parametrize("arguments", [["name"], "name=ok", 42, b"bytes"])
def test_non_object_arguments_rejected(registry, arguments):
    register_echo(registry)
    with pytest.raises(ToolValidationError, match="JSON object"):
        registry.validate_arguments("echo", arguments)


def test_enum_and_pattern_and_nested_validation(registry):
    registry.register(
        "nested_tool",
        "Nested",
        {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["fast", "safe"]},
                "identifier": {"type": "string", "pattern": r"^[A-Z]{3}-\d{2}$"},
                "metadata": {
                    "type": "object",
                    "properties": {"count": {"type": "integer", "minimum": 0}},
                    "required": ["count"],
                },
                "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
        },
        lambda **kwargs: {"success": True},
    )

    valid = {
        "mode": "fast",
        "identifier": "ABC-12",
        "metadata": {"count": 0},
        "tags": ["a"],
    }
    assert registry.validate_arguments("nested_tool", valid) == valid

    with pytest.raises(ToolValidationError, match="must be one of"):
        registry.validate_arguments("nested_tool", {**valid, "mode": "turbo"})
    with pytest.raises(ToolValidationError, match="must match pattern"):
        registry.validate_arguments("nested_tool", {**valid, "identifier": "abc-12"})
    with pytest.raises(ToolValidationError, match="required field is missing"):
        registry.validate_arguments("nested_tool", {**valid, "metadata": {}})
    with pytest.raises(ToolValidationError, match="unexpected field"):
        registry.validate_arguments(
            "nested_tool", {**valid, "metadata": {"count": 1, "extra": True}}
        )
    with pytest.raises(ToolValidationError, match="at least 1 item"):
        registry.validate_arguments("nested_tool", {**valid, "tags": []})


def test_enum_does_not_treat_true_as_one(registry):
    registry.register(
        "numbers",
        "Numbers",
        {"type": "object", "properties": {"value": {"type": "integer", "enum": [1, 2]}}},
        lambda **kwargs: {"success": True},
    )

    with pytest.raises(ToolValidationError):
        registry.validate_arguments("numbers", {"value": True})


def test_validate_unknown_tool_raises(registry):
    with pytest.raises(ToolNotFoundError):
        registry.validate_arguments("missing", {})


def test_validation_errors_are_reported_by_execute_without_calling_handler(registry):
    handler = MagicMock(return_value={"success": True})
    registry.register(
        "covered", "Covered", simple_schema(), handler
    )

    result = registry.execute("covered", {"count": 1})

    assert result.success is False
    assert result.code == "validation_error"
    handler.assert_not_called()


# ---------------------------------------------------------------------------
# Permission enforcement
# ---------------------------------------------------------------------------


def test_read_only_tool_runs_without_confirmation(registry):
    handler = MagicMock(return_value={"success": True, "message": "read"})
    registry.register("read_tool", "Reads", {}, handler, permission=Permission.READ_ONLY)

    result = registry.execute("read_tool", {})

    assert result.success is True
    assert result.code == "ok"
    handler.assert_called_once_with()


def test_reversible_tool_runs_without_confirmation(registry):
    handler = MagicMock(return_value={"success": True})
    registry.register("reversible_tool", "Undoable", {}, handler, permission="REVERSIBLE")

    assert registry.execute("reversible_tool", {}).success is True
    handler.assert_called_once_with()


def test_confirmation_required_tool_refuses_without_grant():
    registry, _broker, _approver, calls = make_confirmation_registry()

    result = registry.execute("dangerous_action", {"target": "prod"})

    assert result.success is False
    assert result.code == "confirmation_required"
    assert calls == []


def test_confirmation_required_tool_runs_with_valid_grant():
    registry, broker, _approver, calls = make_confirmation_registry()

    args = {"target": "prod"}
    grant = broker.request("dangerous_action", args)
    assert isinstance(grant, ConfirmationGrant)

    result = registry.execute("dangerous_action", args, confirmation=grant)

    assert result.success is True
    assert calls == [args]


def test_grant_for_a_different_tool_is_rejected():
    registry, broker, _approver, calls = make_confirmation_registry()
    registry.register(
        "other_action",
        "Another one",
        {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
        lambda **kwargs: {"success": True},
        permission=Permission.CONFIRMATION_REQUIRED,
    )

    grant = broker.request("dangerous_action", {"target": "prod"})
    result = registry.execute("other_action", {"target": "prod"}, confirmation=grant)

    assert result.success is False
    assert result.code == "confirmation_invalid"
    assert calls == []


def test_grant_for_a_different_registry_is_rejected():
    """A grant must be scoped to the registry that minted it."""
    registry_a = ToolRegistry("registry-a")
    registry_b = ToolRegistry("registry-b")

    executed = []
    for target_registry in (registry_a, registry_b):
        target_registry.register(
            "dangerous_action",
            "Does a consequential thing",
            {"type": "object", "properties": {"target": {"type": "string"}}},
            lambda **kwargs: executed.append(kwargs) or {"success": True},
            permission=Permission.CONFIRMATION_REQUIRED,
        )

    broker = ConfirmationBroker(registry_a, lambda request: True)
    grant = broker.request("dangerous_action", {"target": "prod"})

    result = registry_b.execute("dangerous_action", {"target": "prod"}, confirmation=grant)

    assert result.success is False
    assert result.code == "confirmation_invalid"
    assert "different registry" in result.message
    assert executed == []

    # The same grant still works against its originating registry.
    assert registry_a.execute("dangerous_action", {"target": "prod"}, confirmation=grant).success is True
    assert executed == [{"target": "prod"}]


def test_grant_for_different_arguments_is_rejected():
    registry, broker, _approver, calls = make_confirmation_registry()

    grant = broker.request("dangerous_action", {"target": "staging"})
    result = registry.execute("dangerous_action", {"target": "prod"}, confirmation=grant)

    assert result.success is False
    assert result.code == "confirmation_invalid"
    assert calls == []


def test_grant_cannot_be_replayed():
    registry, broker, _approver, calls = make_confirmation_registry()

    args = {"target": "prod"}
    grant = broker.request("dangerous_action", args)

    first = registry.execute("dangerous_action", args, confirmation=grant)
    second = registry.execute("dangerous_action", args, confirmation=grant)

    assert first.success is True
    assert second.success is False
    assert second.code == "confirmation_invalid"
    assert len(calls) == 1


def test_expired_grant_is_rejected():
    registry, broker, _approver, calls = make_confirmation_registry(ttl_seconds=0.02)

    args = {"target": "prod"}
    grant = broker.request("dangerous_action", args)
    time.sleep(0.05)

    result = registry.execute("dangerous_action", args, confirmation=grant)

    assert result.success is False
    assert result.code == "confirmation_invalid"
    assert "expired" in result.message.lower()
    assert calls == []


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ConfirmationGrant(tool_name="x", fingerprint="a" * 64, ttl_seconds=30),
        lambda: ConfirmationGrant(
            tool_name="x", fingerprint="a" * 64, ttl_seconds=30, _key=object()
        ),
        lambda: ConfirmationGrant(tool_name="", fingerprint="a" * 64, ttl_seconds=30),
        lambda: ConfirmationGrant(
            tool_name="x", fingerprint="a" * 64, ttl_seconds=30, _key=None
        ),
    ],
)
def test_forged_grants_cannot_be_constructed(factory):
    # Even the first two arguments are public; without the module-private mint
    # sentinel every direct construction must fail.
    with pytest.raises(ConfirmationError):
        factory()


def test_grant_is_refused_for_read_only_tool():
    registry, broker, _approver, _calls = make_confirmation_registry()
    registry.register("read_only_tool", "Reads", {}, lambda: {"success": True})

    grant = broker.request("dangerous_action", {"target": "prod"})
    result = registry.execute("read_only_tool", {}, confirmation=grant)

    assert result.success is False
    assert result.code == "confirmation_invalid"
    assert "does not require confirmation" in result.message


def test_confirmation_grant_metadata():
    registry, broker, _approver, _calls = make_confirmation_registry(ttl_seconds=60)

    grant = broker.request("dangerous_action", {"target": "prod"})

    assert grant.tool_name == "dangerous_action"
    assert grant.used is False
    assert grant.expires_at > grant.issued_at
    assert "dangerous_action" in repr(grant)


# ---------------------------------------------------------------------------
# Confirmation broker: trusted application-side approval only
# ---------------------------------------------------------------------------


def test_broker_passes_full_request_to_approver():
    registry, broker, approver, _calls = make_confirmation_registry()

    broker.request("dangerous_action", {"target": "prod"})

    approver.assert_called_once()
    request = approver.call_args.args[0]
    assert isinstance(request, ConfirmationRequest)
    assert request.tool_name == "dangerous_action"
    assert request.permission is Permission.CONFIRMATION_REQUIRED
    assert request.arguments["target"] == "prod"
    assert "prod" in request.summary()


def test_broker_returns_none_when_denied():
    registry, broker, _approver, calls = make_confirmation_registry(approved=False)

    grant = broker.request("dangerous_action", {"target": "prod"})

    assert grant is None
    assert registry.execute("dangerous_action", {"target": "prod"}).code == "confirmation_required"
    assert calls == []


@pytest.mark.parametrize("approval_value", ["yes", 1, {"ok": True}, [], object()])
def test_broker_requires_literal_true(approval_value):
    registry = ToolRegistry("strict-approval")
    registry.register(
        "dangerous_action",
        "Does a consequential thing",
        {"type": "object", "properties": {"target": {"type": "string"}}},
        lambda **kwargs: {"success": True},
        permission=Permission.CONFIRMATION_REQUIRED,
    )
    broker = ConfirmationBroker(registry, lambda request: approval_value)

    assert broker.request("dangerous_action", {"target": "prod"}) is None


def test_broker_fails_closed_when_approver_raises():
    registry = ToolRegistry("failing-approver")
    registry.register(
        "dangerous_action",
        "Does a consequential thing",
        {"type": "object", "properties": {"target": {"type": "string"}}},
        lambda **kwargs: {"success": True},
        permission=Permission.CONFIRMATION_REQUIRED,
    )

    def explode(request):
        raise RuntimeError("dialog crashed")

    broker = ConfirmationBroker(registry, explode)
    assert broker.request("dangerous_action", {"target": "prod"}) is None


def test_broker_rejects_tool_that_does_not_need_confirmation(registry):
    register_echo(registry)
    broker = ConfirmationBroker(registry, lambda request: True)

    with pytest.raises(ConfirmationError, match="does not require confirmation"):
        broker.request("echo", {"name": "ok"})


def test_broker_does_not_ask_approver_for_invalid_arguments():
    registry, broker, approver, _calls = make_confirmation_registry()

    with pytest.raises(ToolValidationError):
        broker.request("dangerous_action", {})

    approver.assert_not_called()


def test_broker_validates_ttl_and_dependencies():
    registry, _broker, _approver, _calls = make_confirmation_registry()

    with pytest.raises(ValueError):
        ConfirmationBroker(registry, lambda request: True, ttl_seconds=0)
    with pytest.raises(TypeError):
        ConfirmationBroker("not-a-registry", lambda request: True)
    with pytest.raises(TypeError):
        ConfirmationBroker(registry, "not-callable")


# ---------------------------------------------------------------------------
# Confirmation spoofing through model-supplied arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spoof",
    [
        {"user_confirmed": True},
        {"app_confirmed": True},
        {"confirmed": True},
        {"is_confirmed": True},
        {"approval": "yes"},
        {"approved": True},
        {"confirmation_token": "abc"},
        {"confirmation_grant": {}},
        {"requires_confirmation": False},
    ],
)
def test_confirmation_fields_in_arguments_are_rejected(spoof):
    registry, _broker, _approver, calls = make_confirmation_registry()
    arguments = {"target": "prod", **spoof}

    result = registry.execute("dangerous_action", arguments)

    assert result.success is False
    assert result.code == "confirmation_spoof"
    assert calls == []
    with pytest.raises(ConfirmationSpoofError):
        registry.validate_arguments("dangerous_action", arguments)


def test_spoofed_arguments_do_not_mint_a_grant():
    registry, broker, approver, _calls = make_confirmation_registry()

    with pytest.raises(ConfirmationSpoofError):
        broker.request("dangerous_action", {"target": "prod", "user_confirmed": True})

    approver.assert_not_called()


# ---------------------------------------------------------------------------
# Execution results
# ---------------------------------------------------------------------------


def test_handler_may_receive_explicit_agent_context(registry):
    observed = []
    sentinel = object()

    def context_aware(text: str, _agent_context=None):
        observed.append((text, _agent_context))
        return {"success": True}

    registry.register(
        "context_tool",
        "Context tool",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        context_aware,
    )

    result = registry.execute(
        "context_tool", {"text": "hello"}, context=sentinel
    )

    assert result.success is True
    assert observed == [("hello", sentinel)]


def test_agent_context_is_not_injected_into_ordinary_handlers(registry):
    observed = []
    registry.register(
        "ordinary_tool",
        "Ordinary",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        lambda text: observed.append(text) or {"success": True},
    )

    assert registry.execute(
        "ordinary_tool", {"text": "hello"}, context=object()
    ).success is True
    assert observed == ["hello"]


def test_handler_may_return_tool_result(registry):
    expected = ToolResult(success=True, tool_name="", message="done", data={"x": 1})
    registry.register("returns_result", "Returns ToolResult", {}, lambda: expected)

    result = registry.execute("returns_result", {})

    assert result.success is True
    assert result.tool_name == "returns_result"
    assert result.message == "done"
    assert result.data == {"x": 1}
    assert result.duration_ms >= 0


def test_handler_may_return_plain_value(registry):
    registry.register("returns_value", "Returns string", {}, lambda: "finished")

    result = registry.execute("returns_value", {})

    assert result.success is True
    assert result.message == "finished"


def test_handler_failure_result_is_preserved(registry):
    registry.register(
        "fails",
        "Fails explicitly",
        {},
        lambda: {"success": False, "message": "refused", "code": "not_supported"},
    )

    result = registry.execute("fails", {})

    assert result.success is False
    assert result.code == "not_supported"
    assert result.message == "refused"


def test_handler_tool_result_without_error_gets_a_code(registry):
    registry.register(
        "fails_quietly",
        "Fails",
        {},
        lambda: ToolResult(success=False, tool_name="fails_quietly", message="bad"),
    )

    assert registry.execute("fails_quietly", {}).code == "tool_error"


def test_handler_exception_is_structured_and_redacted(registry):
    def explode(**kwargs):
        raise RuntimeError("backend rejected api_key=SUPERSECRETVALUE")

    registry.register("explodes", "Explodes", {}, explode)

    result = registry.execute("explodes", {})

    assert result.success is False
    assert result.code == "execution_error"
    assert result.error is not None
    assert "SUPERSECRETVALUE" not in result.error
    assert "[REDACTED]" in result.error


@pytest.mark.parametrize(
    "bad_return",
    [
        {"message": "no success flag"},
        {"success": "true"},
        {"success": 1},
        {"success": True, "extra": 1},
        {"success": True, "data": ["not", "an", "object"]},
        {"success": True, "message": 5},
        {"success": True, "code": 3},
        {"success": False, "error": {"nested": True}},
    ],
)
def test_malformed_handler_results_are_errors(registry, bad_return):
    registry.register("malformed", "Malformed", {}, lambda: bad_return)

    result = registry.execute("malformed", {})

    assert result.success is False
    assert result.code == "execution_error"


def test_result_to_dict_shape(registry):
    registry.register("ok_tool", "OK", {}, lambda: {"success": True, "message": "ok"})

    payload = registry.execute("ok_tool", {}).to_dict()

    assert set(payload) == {
        "success",
        "tool_name",
        "message",
        "data",
        "error",
        "code",
        "duration_ms",
    }


def test_execute_does_not_raise_on_validation_failure(registry):
    register_echo(registry)
    result = registry.execute("echo", {"count": "bad"})
    assert result.success is False
    assert result.code == "validation_error"


def test_unregister_and_clear(registry):
    register_echo(registry, "one")
    register_echo(registry, "two")

    assert registry.unregister("one") is True
    assert registry.unregister("one") is False
    assert registry.names() == ("two",)

    registry.clear()
    assert len(registry) == 0
    assert registry.names() == ()


# ---------------------------------------------------------------------------
# Isolation, default registry and audit trail
# ---------------------------------------------------------------------------


def test_registries_are_isolated():
    first = ToolRegistry("first")
    second = ToolRegistry("second")
    register_echo(first)
    register_echo(second, name="other")

    first.clear()

    assert first.names() == ()
    assert second.names() == ("other",)


def test_default_registry_is_lazy_and_stable():
    import engine.tool_registry as module

    module._DEFAULT_REGISTRY = None
    assert get_default_registry() is get_default_registry()
    assert get_default_registry().name == "default"


def test_import_has_no_side_effects():
    """Importing the package must not create a registry, import Qt or touch disk."""
    code = (
        "import sys\n"
        "import engine\n"
        "import engine.tool_registry as t\n"
        "print(t._DEFAULT_REGISTRY)\n"
        "print('PySide6' in sys.modules)\n"
        "print(len(engine.get_default_registry().names()))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert lines[0] == "None", "importing must not construct the default registry"
    assert lines[1] == "False", "engine must not import PySide6"
    assert lines[2] == "0", "importing must not register tools"


def test_audit_log_has_metadata_but_no_argument_values(registry):
    def fails(**kwargs):
        raise RuntimeError("nope")

    registry.register("audited", "Audited", simple_schema(), fails)
    registry.execute("audited", {"name": "TOPSECRETNAME", "count": 2})

    audit = registry.get_audit_log()
    assert len(audit) == 1
    entry = audit[0]

    assert entry["tool"] == "audited"
    assert entry["permission"] == "READ_ONLY"
    assert entry["success"] is False
    assert entry["code"] == "execution_error"
    assert len(entry["arguments_fingerprint"]) == 16
    assert "TOPSECRETNAME" not in json.dumps(audit)
    assert "nope" not in json.dumps(audit)


def test_audit_log_is_bounded(registry):
    registry.register("noop", "Noop", {}, lambda: {"success": True})

    for _ in range(registry.MAX_AUDIT_ENTRIES + 5):
        registry.execute("noop", {})

    assert len(registry.get_audit_log()) == registry.MAX_AUDIT_ENTRIES


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_registration_of_unique_tools(registry):
    barrier = threading.Barrier(16)
    errors = []

    def register(index):
        try:
            barrier.wait(timeout=10)
            registry.register(
                f"tool_{index:02d}",
                f"Tool {index}",
                {},
                lambda index=index: {"success": True, "message": str(index)},
            )
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)

    threads = [threading.Thread(target=register, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(registry) == 16


def test_concurrent_duplicate_registration_has_exactly_one_winner(registry):
    barrier = threading.Barrier(8)
    outcomes = []
    lock = threading.Lock()

    def attempt():
        barrier.wait(timeout=10)
        try:
            registry.register("contended", "Contended", {}, lambda: {"success": True})
            with lock:
                outcomes.append("ok")
        except ToolRegistrationError:
            with lock:
                outcomes.append("duplicate")

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert outcomes.count("ok") == 1
    assert outcomes.count("duplicate") == 7


def test_concurrent_use_of_one_grant_executes_at_most_once():
    registry, broker, _approver, calls = make_confirmation_registry()
    args = {"target": "prod"}
    grant = broker.request("dangerous_action", args)

    barrier = threading.Barrier(8)
    results = []
    lock = threading.Lock()

    def attempt():
        barrier.wait(timeout=10)
        result = registry.execute("dangerous_action", args, confirmation=grant)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sum(1 for result in results if result.success) == 1
    assert sum(1 for result in results if result.code == "confirmation_invalid") == 7
    assert calls == [args]
