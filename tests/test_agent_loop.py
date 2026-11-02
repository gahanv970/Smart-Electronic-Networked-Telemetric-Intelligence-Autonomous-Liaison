"""Offline tests for the Phase 2 agent loop.

No network, Qt, Gemini or real external service is used here.  The model is a
scripted in-process object and every tool handler is local.
"""

from __future__ import annotations

import threading
import time
from typing import Any, List, Optional, Sequence

import pytest

from engine.agent_loop import (
    AgentConfig,
    AgentConfigurationError,
    AgentError,
    AgentMessage,
    AgentRunner,
    AgentState,
    AgentToolContext,
    CancellationToken,
    ModelResponse,
    ToolCall,
)
from engine.tool_registry import (
    ConfirmationBroker,
    Permission,
    ToolRegistry,
    ToolSpec,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ScriptedModel:
    """Returns pre-scripted responses and records every call."""

    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.calls: List[dict] = []
        self._lock = threading.Lock()

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    def generate(
        self,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolSpec],
        *,
        system_instruction: str,
        timeout_seconds: Optional[float],
        cancellation: Optional[CancellationToken] = None,
    ) -> ModelResponse:
        with self._lock:
            self.calls.append(
                {
                    "messages": list(messages),
                    "tools": list(tools),
                    "system_instruction": system_instruction,
                    "timeout_seconds": timeout_seconds,
                    "cancellation": cancellation,
                }
            )
            if not self._responses:
                raise AssertionError("ScriptedModel ran out of responses.")
            item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class BlockingModel:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def generate(self, messages, tools, **kwargs):
        self.calls += 1
        self.started.set()
        if not self.release.wait(5.0):
            raise AssertionError("BlockingModel was not released.")
        return ModelResponse(text="late")


def text_response(text: str = "done") -> ModelResponse:
    return ModelResponse(text=text)


def tool_response(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(tool_calls=tuple(calls))


def call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(name=name, arguments=arguments)


def make_registry():
    registry = ToolRegistry("agent-loop-test")
    executed: List[dict] = []

    def echo_text(text: str):
        executed.append({"tool": "echo_text", "text": text})
        return {
            "success": True,
            "message": f"echoed {text}",
            "data": {"text": text},
        }

    def read_value(key: str):
        executed.append({"tool": "read_value", "key": key})
        return {"success": True, "data": {"key": key, "value": 42}}

    def delete_item(target: str):
        executed.append({"tool": "delete_item", "target": target})
        return {"success": True, "message": f"deleted {target}"}

    registry.register(
        "echo_text",
        "Echo text back",
        {
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
        },
        echo_text,
    )
    registry.register(
        "read_value",
        "Read a value",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
        read_value,
        permission=Permission.READ_ONLY,
    )
    registry.register(
        "delete_item",
        "Delete one item permanently",
        {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
        delete_item,
        permission=Permission.CONFIRMATION_REQUIRED,
    )
    return registry, executed


def run(registry, responses, *, config=None, broker=None, listener=None):
    model = ScriptedModel(responses)
    runner = AgentRunner(
        registry,
        model,
        config=config,
        confirmation_broker=broker,
        listener=listener,
    )
    result = runner.run("do the thing")
    return result, model, runner


# ---------------------------------------------------------------------------
# Multi-step execution and history
# ---------------------------------------------------------------------------


def test_multi_step_tool_execution_completes():
    registry, executed = make_registry()
    result, model, runner = run(
        registry,
        [tool_response(call("echo_text", text="one")), text_response("finished")],
    )

    assert result.state is AgentState.COMPLETED
    assert result.success is True
    assert result.final_text == "finished"
    assert result.turns == 2
    assert result.tool_call_count == 1
    assert executed == [{"tool": "echo_text", "text": "one"}]
    assert model.call_count == 2
    assert runner.state is AgentState.COMPLETED

    second_messages = model.calls[1]["messages"]
    assert [message.role for message in second_messages] == ["user", "model", "tool"]
    assert second_messages[1].tool_calls[0].name == "echo_text"
    assert second_messages[2].tool_responses[0].result["success"] is True
    assert second_messages[2].tool_responses[0].result["data"] == {"text": "one"}


def test_model_receives_all_registered_tool_specs():
    registry, _executed = make_registry()
    _result, model, _runner = run(registry, [text_response()])

    names = [spec.name for spec in model.calls[0]["tools"]]
    assert names == ["echo_text", "read_value", "delete_item"]
    assert all(isinstance(spec, ToolSpec) for spec in model.calls[0]["tools"])


def test_requested_tools_are_executed_sequentially_not_in_parallel():
    registry = ToolRegistry("sequential")
    events: List[tuple] = []
    lock = threading.Lock()

    def handler(name):
        def run_tool(**kwargs):
            with lock:
                events.append((name, "start"))
            time.sleep(0.05)
            with lock:
                events.append((name, "end"))
            return {"success": True}

        return run_tool

    for name in ("first_tool", "second_tool"):
        registry.register(
            name,
            name,
            {"type": "object", "properties": {"value": {"type": "string"}}},
            handler(name),
        )

    result, _model, _runner = run(
        registry,
        [
            tool_response(
                call("first_tool", value="a"),
                call("second_tool", value="b"),
            ),
            text_response("both done"),
        ],
    )

    assert result.success is True
    assert events == [
        ("first_tool", "start"),
        ("first_tool", "end"),
        ("second_tool", "start"),
        ("second_tool", "end"),
    ]


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_tool_call_limit_rejects_the_next_call():
    registry, executed = make_registry()
    config = AgentConfig(max_tool_calls=2, max_turns=10)

    result, model, _runner = run(
        registry,
        [
            tool_response(call("echo_text", text="one")),
            tool_response(call("echo_text", text="two")),
            tool_response(call("echo_text", text="three")),
            text_response("should not be reached"),
        ],
        config=config,
    )

    assert result.state is AgentState.FAILED
    assert result.error_code == "tool_call_limit"
    assert result.tool_call_count == 2
    assert len(executed) == 2
    assert model.call_count == 3


def test_max_turns_limit():
    registry, executed = make_registry()
    config = AgentConfig(max_turns=1, max_tool_calls=8)

    result, _model, _runner = run(
        registry,
        [
            tool_response(call("echo_text", text="one")),
            text_response("unreachable"),
        ],
        config=config,
    )

    assert result.state is AgentState.FAILED
    assert result.error_code == "max_turns"
    assert result.tool_call_count == 1
    assert len(executed) == 1


def test_workflow_timeout_during_model_call():
    registry, _executed = make_registry()
    model = BlockingModel()
    runner = AgentRunner(
        registry,
        model,
        config=AgentConfig(workflow_timeout_seconds=0.2, tool_timeout_seconds=5),
    )

    try:
        result = runner.run("wait forever")
    finally:
        model.release.set()

    assert result.state is AgentState.FAILED
    assert result.error_code == "workflow_timeout"
    assert result.elapsed_seconds < 2.0


def test_workflow_timeout_during_tool_call():
    registry = ToolRegistry("workflow-timeout")
    started = threading.Event()
    release = threading.Event()

    def slow(**kwargs):
        started.set()
        release.wait(5.0)
        return {"success": True}

    registry.register(
        "slow_tool",
        "Slow",
        {"type": "object", "properties": {}},
        slow,
    )
    runner = AgentRunner(
        registry,
        ScriptedModel([tool_response(call("slow_tool")), text_response()]),
        config=AgentConfig(
            workflow_timeout_seconds=0.2,
            tool_timeout_seconds=5.0,
        ),
    )

    try:
        result = runner.run("slow")
    finally:
        release.set()

    assert started.is_set()
    assert result.state is AgentState.FAILED
    assert result.error_code == "workflow_timeout"
    assert result.tool_results[0].code == "workflow_timeout"


def test_individual_tool_timeout_does_not_report_success():
    registry = ToolRegistry("tool-timeout")
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow(**kwargs):
        started.set()
        release.wait(5.0)
        finished.set()
        return {"success": True, "message": "the handler eventually finished"}

    registry.register(
        "slow_tool",
        "Slow",
        {"type": "object", "properties": {}},
        slow,
    )
    runner = AgentRunner(
        registry,
        ScriptedModel([tool_response(call("slow_tool")), text_response("unreachable")]),
        config=AgentConfig(tool_timeout_seconds=0.1, workflow_timeout_seconds=5.0),
    )

    try:
        result = runner.run("slow")
    finally:
        release.set()
        finished.wait(2.0)

    assert started.is_set()
    assert result.state is AgentState.FAILED
    assert result.error_code == "tool_timeout"
    assert result.tool_results[0].success is False
    assert result.tool_results[0].code == "tool_timeout"
    assert result.final_text == ""


def test_tool_specific_timeout_can_only_lower_the_global_budget():
    registry = ToolRegistry("tool-specific-timeout")
    started = threading.Event()
    release = threading.Event()

    def slow(**kwargs):
        started.set()
        release.wait(5.0)
        return {"success": True}

    registry.register(
        "slow_tool",
        "Slow",
        {"type": "object", "properties": {}},
        slow,
        timeout_seconds=0.05,
    )
    runner = AgentRunner(
        registry,
        ScriptedModel([tool_response(call("slow_tool")), text_response()]),
        config=AgentConfig(tool_timeout_seconds=10.0, workflow_timeout_seconds=30.0),
    )

    try:
        result = runner.run("slow")
    finally:
        release.set()

    assert started.is_set()
    assert result.error_code == "tool_timeout"
    assert result.elapsed_seconds < 2.0


def test_agent_config_defaults_and_validation():
    defaults = AgentConfig()
    assert defaults.max_tool_calls == 8
    assert defaults.tool_timeout_seconds == 60.0
    assert defaults.workflow_timeout_seconds == 300.0

    from_mapping = AgentConfig.from_mapping(
        {
            "agent_max_tool_calls": 3,
            "agent_tool_timeout_seconds": 12,
            "agent_workflow_timeout_seconds": 90,
            "agent_max_turns": 4,
            "unrelated": True,
        }
    )
    assert from_mapping.max_tool_calls == 3
    assert from_mapping.tool_timeout_seconds == 12.0
    assert from_mapping.workflow_timeout_seconds == 90.0
    assert from_mapping.max_turns == 4

    for kwargs in (
        {"max_tool_calls": 0},
        {"max_tool_calls": True},
        {"max_tool_calls": "8"},
        {"max_turns": 0},
        {"tool_timeout_seconds": 0},
        {"tool_timeout_seconds": False},
        {"workflow_timeout_seconds": -1},
        {"workflow_timeout_seconds": "300"},
    ):
        with pytest.raises(AgentConfigurationError):
            AgentConfig(**kwargs)


def test_agent_config_reads_the_central_application_defaults():
    from config import DEFAULT_CONFIG

    config = AgentConfig.from_mapping(DEFAULT_CONFIG)

    assert config.max_tool_calls == 8
    assert config.tool_timeout_seconds == 60.0
    assert config.workflow_timeout_seconds == 300.0
    assert config.max_turns == 12


def test_second_concurrent_run_is_rejected_immediately():
    registry, _executed = make_registry()
    model = BlockingModel()
    runner = AgentRunner(registry, model, config=AgentConfig(workflow_timeout_seconds=30))
    box = {}
    started = threading.Event()

    def run_it():
        started.set()
        box["result"] = runner.run("first")

    thread = threading.Thread(target=run_it)
    thread.start()
    assert started.wait(1.0)
    assert model.started.wait(2.0)

    try:
        with pytest.raises(AgentError, match="already running"):
            runner.run("second")
    finally:
        runner.cancel("test cleanup")
        model.release.set()
        thread.join(5.0)

    assert box["result"].state in {AgentState.COMPLETED, AgentState.CANCELLED}


# ---------------------------------------------------------------------------
# Validation and failure reporting
# ---------------------------------------------------------------------------


def test_invalid_tool_name_and_arguments_are_reported_and_model_recovers():
    registry, executed = make_registry()

    result, model, _runner = run(
        registry,
        [
            tool_response(call("no_such_tool", anything="x")),
            tool_response(call("echo_text")),
            text_response("I could not run those tools."),
        ],
    )

    assert result.state is AgentState.COMPLETED
    assert result.success is True
    assert [item.code for item in result.tool_results] == [
        "not_found",
        "validation_error",
    ]
    assert executed == []
    assert model.call_count == 3
    assert len(result.failed_tool_results) == 2


def test_invalid_arguments_are_never_sent_to_the_handler():
    registry, executed = make_registry()

    result, _model, _runner = run(
        registry,
        [
            tool_response(call("echo_text", text=123)),
            text_response("bad arguments were rejected"),
        ],
    )

    assert result.state is AgentState.COMPLETED
    assert result.tool_results[0].code == "validation_error"
    assert executed == []


def test_tool_exception_is_structured_and_model_recovers():
    registry = ToolRegistry("raises")

    def explode(**kwargs):
        raise RuntimeError("backend exploded")

    registry.register("explode_tool", "Explodes", {}, explode)

    result, _model, _runner = run(
        registry,
        [
            tool_response(call("explode_tool")),
            text_response("The tool failed; nothing was completed."),
        ],
    )

    assert result.state is AgentState.COMPLETED
    assert result.tool_results[0].success is False
    assert result.tool_results[0].code == "execution_error"
    assert result.final_text.startswith("The tool failed")


def test_handler_returning_failure_is_preserved_and_not_retried():
    registry = ToolRegistry("failing")
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        return {"success": False, "message": "refused", "code": "not_supported"}

    registry.register("failing_tool", "Fails", {}, fail)

    result, _model, _runner = run(
        registry,
        [
            tool_response(call("failing_tool")),
            text_response("It was refused."),
        ],
    )

    assert result.state is AgentState.COMPLETED
    assert len(calls) == 1
    assert result.tool_results[0].success is False
    assert result.tool_results[0].code == "not_supported"


def test_model_exception_fails_without_retry():
    registry, _executed = make_registry()
    result, model, _runner = run(
        registry,
        [RuntimeError("network is down")],
    )

    assert result.state is AgentState.FAILED
    assert result.error_code == "model_error"
    assert "network is down" in (result.error or "")
    assert model.call_count == 1


def test_invalid_model_response_object_fails_cleanly():
    registry, _executed = make_registry()
    result, _model, _runner = run(registry, [{"text": "not a ModelResponse"}])

    assert result.state is AgentState.FAILED
    assert result.error_code == "invalid_model_response"


def test_empty_model_response_fails_cleanly():
    registry, _executed = make_registry()
    result, _model, _runner = run(registry, [ModelResponse(text="")])

    assert result.state is AgentState.FAILED
    assert result.error_code == "empty_response"


def test_listener_exception_does_not_break_the_workflow():
    registry, _executed = make_registry()

    def bad_listener(event):
        raise RuntimeError("the UI listener crashed")

    result, _model, _runner = run(
        registry, [text_response("still works")], listener=bad_listener
    )

    assert result.state is AgentState.COMPLETED
    assert result.final_text == "still works"


# ---------------------------------------------------------------------------
# Permissions and confirmation
# ---------------------------------------------------------------------------


def test_reversible_tool_runs_without_confirmation():
    registry = ToolRegistry("reversible")
    calls = []
    registry.register(
        "reversible_tool",
        "Undoable",
        {"type": "object", "properties": {}},
        lambda **kwargs: calls.append("ran") or {"success": True},
        permission=Permission.REVERSIBLE,
    )

    result, _model, _runner = run(
        registry, [tool_response(call("reversible_tool")), text_response()]
    )

    assert result.success is True
    assert calls == ["ran"]


def test_confirmation_required_without_broker_fails_closed():
    registry, executed = make_registry()

    result, _model, _runner = run(
        registry,
        [tool_response(call("delete_item", target="prod")), text_response()],
    )

    assert result.state is AgentState.FAILED
    assert result.error_code == "confirmation_unavailable"
    assert executed == []
    assert result.tool_results[0].code == "confirmation_unavailable"


def test_confirmation_denied_does_not_execute_or_retry():
    registry, executed = make_registry()
    approver_calls = []

    def deny(request):
        approver_calls.append(request)
        return False

    broker = ConfirmationBroker(registry, deny)

    result, model, _runner = run(
        registry,
        [
            tool_response(call("delete_item", target="prod")),
            text_response("unreachable"),
        ],
        broker=broker,
    )

    assert result.state is AgentState.FAILED
    assert result.error_code == "confirmation_denied"
    assert len(approver_calls) == 1
    assert approver_calls[0].tool_name == "delete_item"
    assert executed == []
    assert model.call_count == 1
    assert result.tool_results[0].code == "confirmation_denied"


def test_confirmation_granted_executes_exactly_once():
    registry, executed = make_registry()
    approver_calls = []

    def approve(request):
        approver_calls.append(request)
        return True

    broker = ConfirmationBroker(registry, approve)

    result, _model, _runner = run(
        registry,
        [
            tool_response(call("delete_item", target="prod")),
            text_response("deleted"),
        ],
        broker=broker,
    )

    assert result.state is AgentState.COMPLETED
    assert len(approver_calls) == 1
    assert executed == [{"tool": "delete_item", "target": "prod"}]
    assert result.tool_results[0].success is True


def test_truthy_approver_value_is_not_approval():
    registry, executed = make_registry()
    broker = ConfirmationBroker(registry, lambda request: "yes please")

    result, _model, _runner = run(
        registry,
        [tool_response(call("delete_item", target="prod")), text_response()],
        broker=broker,
    )

    assert result.state is AgentState.FAILED
    assert result.error_code == "confirmation_denied"
    assert executed == []


@pytest.mark.parametrize(
    "spoof",
    [
        {"user_confirmed": True},
        {"app_confirmed": True},
        {"confirmation_token": "abc"},
        {"approval": "yes"},
    ],
)
def test_model_cannot_spoof_confirmation(spoof):
    registry, executed = make_registry()
    approver_calls = []
    broker = ConfirmationBroker(
        registry, lambda request: approver_calls.append(request) or True
    )

    result, model, _runner = run(
        registry,
        [
            tool_response(call("delete_item", target="prod", **spoof)),
            text_response("The action was not authorized."),
        ],
        broker=broker,
    )

    # The workflow may recover with an honest final answer, but the action must
    # never execute and the application approver must never be asked.
    assert result.state is AgentState.COMPLETED
    assert result.tool_results[0].code == "confirmation_spoof"
    assert executed == []
    assert approver_calls == []
    assert model.call_count == 2


def test_confirmation_state_is_visible_to_the_listener():
    registry, _executed = make_registry()
    broker = ConfirmationBroker(registry, lambda request: True)
    events = []

    result, _model, _runner = run(
        registry,
        [
            tool_response(call("delete_item", target="prod")),
            text_response("done"),
        ],
        broker=broker,
        listener=events.append,
    )

    states = [event.state for event in events]
    assert AgentState.PLANNING in states
    assert AgentState.RUNNING in states
    assert AgentState.WAITING_FOR_CONFIRMATION in states
    assert AgentState.COMPLETED in states
    assert any(event.kind == "confirmation_requested" for event in events)
    assert any(event.kind == "confirmation_granted" for event in events)
    assert result.success is True


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancellation_before_execution_calls_neither_model_nor_tool():
    registry, executed = make_registry()
    model = ScriptedModel([text_response("unreachable")])
    token = CancellationToken()
    token.cancel("user closed the window")
    events = []
    runner = AgentRunner(registry, model, listener=events.append)

    result = runner.run("do nothing", cancellation=token)

    assert result.state is AgentState.CANCELLED
    assert result.cancelled is True
    assert result.error_code == "cancelled"
    assert model.call_count == 0
    assert executed == []
    assert runner.state is AgentState.CANCELLED
    states = [event.state for event in events]
    assert AgentState.CANCELLING in states
    assert AgentState.CANCELLED in states


def test_cancellation_during_tool_stops_scheduling_and_reports_abandonment():
    registry = ToolRegistry("cancel-tool")
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    executed = []

    def blocking(**kwargs):
        started.set()
        release.wait(5.0)
        finished.set()
        executed.append("finished after cancellation")
        return {"success": True, "message": "too late"}

    registry.register(
        "blocking_tool",
        "Blocks",
        {"type": "object", "properties": {}},
        blocking,
    )
    model = ScriptedModel(
        [tool_response(call("blocking_tool")), text_response("unreachable")]
    )
    runner = AgentRunner(
        registry,
        model,
        config=AgentConfig(tool_timeout_seconds=10, workflow_timeout_seconds=30),
    )
    box = {}

    def run_it():
        box["result"] = runner.run("block")

    thread = threading.Thread(target=run_it)
    thread.start()
    assert started.wait(2.0)
    assert runner.cancel("user pressed cancel") is True
    thread.join(5.0)

    try:
        assert not thread.is_alive()
        result = box["result"]
        assert result.state is AgentState.CANCELLED
        assert result.error_code == "cancelled"
        assert result.tool_results[0].code == "cancelled"
        assert "abandoned" in result.tool_results[0].message
        assert model.call_count == 1
        assert runner.state is AgentState.CANCELLED
    finally:
        release.set()
        finished.wait(2.0)

    assert executed == ["finished after cancellation"]


def test_cancellation_after_approval_does_not_execute_the_tool():
    registry, executed = make_registry()
    token = CancellationToken()

    def approve_then_cancel(request):
        token.cancel("user closed the window during confirmation")
        return True

    broker = ConfirmationBroker(registry, approve_then_cancel)
    model = ScriptedModel(
        [tool_response(call("delete_item", target="prod")), text_response("unreachable")]
    )
    runner = AgentRunner(registry, model, confirmation_broker=broker)

    result = runner.run("delete", cancellation=token)

    assert result.state is AgentState.CANCELLED
    assert executed == []
    assert model.call_count == 1


def test_tool_can_cooperatively_observe_cancellation_via_context():
    registry = ToolRegistry("cooperative")
    started = threading.Event()
    stopped = threading.Event()
    seen = []

    def cooperative_explicit(_agent_context=None):
        seen.append(_agent_context)
        started.set()
        while not _agent_context.cancellation.is_cancelled:
            time.sleep(0.005)
        stopped.set()
        return {"success": False, "code": "cancelled"}

    registry.register(
        "cooperative_tool",
        "Cooperative",
        {"type": "object", "properties": {}},
        cooperative_explicit,
    )
    runner = AgentRunner(
        registry,
        ScriptedModel([tool_response(call("cooperative_tool")), text_response()]),
        config=AgentConfig(tool_timeout_seconds=10, workflow_timeout_seconds=30),
    )
    box = {}

    def run_it():
        box["result"] = runner.run("cooperate")

    thread = threading.Thread(target=run_it)
    thread.start()
    assert started.wait(2.0)
    assert runner.cancel("stop") is True
    thread.join(5.0)

    assert stopped.wait(2.0)
    assert box["result"].state is AgentState.CANCELLED
    assert len(seen) == 1
    assert isinstance(seen[0], AgentToolContext)
    assert seen[0].cancellation.is_cancelled
    assert seen[0].seconds_remaining >= 0


def test_cancellation_during_model_call_is_responsive():
    registry, executed = make_registry()
    model = BlockingModel()
    runner = AgentRunner(
        registry,
        model,
        config=AgentConfig(workflow_timeout_seconds=30, tool_timeout_seconds=10),
    )
    box = {}

    def run_it():
        box["result"] = runner.run("block in the model")

    thread = threading.Thread(target=run_it)
    thread.start()
    assert model.started.wait(2.0)
    started_at = time.monotonic()
    assert runner.cancel("stop") is True
    thread.join(5.0)

    try:
        assert not thread.is_alive()
        assert time.monotonic() - started_at < 2.0
        assert box["result"].state is AgentState.CANCELLED
        assert box["result"].tool_call_count == 0
        assert executed == []
    finally:
        model.release.set()


def test_cancel_returns_false_when_no_workflow_is_running():
    registry, _executed = make_registry()
    runner = AgentRunner(registry, ScriptedModel([text_response()]))

    assert runner.cancel() is False
