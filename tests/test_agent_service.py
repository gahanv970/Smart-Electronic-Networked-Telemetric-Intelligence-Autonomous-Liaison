"""Offline tests for the Qt-facing agent application service.

The fake provider never creates a real Gemini client.  Scripted models run in
the executor thread, so these tests also verify that the Qt/asyncio thread is
not used for the blocking agent loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, List, Optional, Sequence

import pytest
from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QApplication

from engine.agent_loop import (
    AgentConfig,
    AgentMessage,
    AgentRunResult,
    AgentState,
    CancellationToken,
    ModelResponse,
    ToolCall,
)
from engine.agent_service import (
    AgentService,
    AgentServiceBusyError,
    AgentServiceUnavailable,
)
from engine.tool_registry import ToolSpec


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class FakeTaskService:
    def __init__(self) -> None:
        self.created: List[dict] = []
        self.get_started = threading.Event()
        self.get_release = threading.Event()
        self.block_get = False
        self.get_error: Optional[BaseException] = None
        self.next_id = 1

    def create_task(self, title, description="", priority="MEDIUM", due_date=None):
        task = {
            "id": self.next_id,
            "title": title,
            "description": description,
            "priority": priority,
            "status": "pending",
            "due_date": due_date,
        }
        self.next_id += 1
        self.created.append(task)
        return dict(task)

    def get_task(self, task_id):
        if self.block_get:
            self.get_started.set()
            if not self.get_release.wait(5.0):
                raise AssertionError("Blocked get_task was not released.")
        if self.get_error is not None:
            raise self.get_error
        if task_id == 1:
            return {
                "id": 1,
                "title": "Review Phase 3",
                "description": "",
                "priority": "HIGH",
                "status": "pending",
                "due_date": None,
            }
        return None

    def get_all_tasks(self, status_filter=None, priority_filter=None, search_query=None, sort_by="created_desc"):
        return []


class FakeMemoryService:
    def build_context_for_prompt(self, query=None):
        return None

    def get_all_memories(self, category=None, search_query=None):
        return []


class FakeAutomationService:
    def get_system_status(self):
        return SimpleNamespace(success=True, message="ok", data={"os": "Windows"})


class FakeBrowserService:
    def navigate_url(self, url, user_confirmed=False):
        return SimpleNamespace(
            success=True,
            message="Browser launch requested.",
            data={"url": url, "launch_accepted": True},
        )


_DEFAULT_CLIENT = object()


class FakeProvider:
    def __init__(
        self,
        *,
        configured: bool = True,
        modern: bool = True,
        client: Any = _DEFAULT_CLIENT,
        model: str = "test-model",
    ) -> None:
        self.configured = configured
        self.modern = modern
        self.client = _DEFAULT_CLIENT if client is _DEFAULT_CLIENT else client
        if self.client is _DEFAULT_CLIENT:
            self.client = object()
        self.model = model
        self.client_calls = 0

    def is_configured(self):
        return self.configured

    def uses_modern_sdk(self):
        return self.modern

    def get_modern_client(self):
        self.client_calls += 1
        return self.client

    def get_current_model(self):
        return self.model


class ScriptedModel:
    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.calls: List[dict] = []
        self.thread_ids: List[int] = []
        self._lock = threading.Lock()

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
            self.thread_ids.append(threading.get_ident())
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
        return ModelResponse(text="finished")


class ConfirmationReceiver(QObject):
    def __init__(self, service: AgentService, decision: Optional[bool]) -> None:
        super().__init__()
        self.service = service
        self.decision = decision
        self.prompts = []

    @Slot(object)
    def on_prompt(self, prompt) -> None:
        self.prompts.append(prompt)
        if self.decision is not None:
            self.service.resolve_confirmation(prompt.request_id, self.decision)


class ResultReceiver(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.results = []

    @Slot(object)
    def on_result(self, result) -> None:
        self.results.append(result)


def make_service(
    model,
    *,
    provider: Optional[FakeProvider] = None,
    task_service: Optional[FakeTaskService] = None,
    confirmation_timeout: float = 2.0,
) -> AgentService:
    provider = provider or FakeProvider()
    task_service = task_service or FakeTaskService()
    return AgentService(
        ai_provider=provider,
        task_service=task_service,
        memory_service=FakeMemoryService(),
        automation_service=FakeAutomationService(),
        browser_service=FakeBrowserService(),
        config=AgentConfig(
            max_tool_calls=4,
            tool_timeout_seconds=2.0,
            workflow_timeout_seconds=10.0,
            max_turns=4,
        ),
        model_factory=lambda client, model_name: model,
        confirmation_timeout_seconds=confirmation_timeout,
    )


async def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Condition was not met before the timeout.")
        await asyncio.sleep(0.01)


async def pump_until(qapp, predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Qt condition was not met before the timeout.")
        qapp.processEvents()
        await asyncio.sleep(0.01)
    qapp.processEvents()


@pytest.mark.asyncio
async def test_missing_api_key_is_unavailable_without_model_call(qapp):
    model = ScriptedModel([])
    provider = FakeProvider(configured=False)
    service = make_service(model, provider=provider)
    try:
        assert service.is_available() is False
        reason = service.unavailable_reason()
        assert "API key" in reason
        with pytest.raises(AgentServiceUnavailable):
            await service.run("hello")
        assert model.calls == []
        assert provider.client_calls == 0
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_legacy_only_sdk_is_reported_as_unavailable(qapp):
    model = ScriptedModel([])
    provider = FakeProvider(configured=True, modern=False, client=None)
    service = make_service(model, provider=provider)
    try:
        assert service.is_available() is False
        assert "legacy" in (service.unavailable_reason() or "").lower()
        with pytest.raises(AgentServiceUnavailable):
            await service.run("hello")
        assert model.calls == []
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_modern_sdk_without_initialized_client_is_unavailable(qapp):
    model = ScriptedModel([])
    provider = FakeProvider(configured=True, modern=True, client=None)
    service = make_service(model, provider=provider)
    try:
        assert service.is_available() is False
        assert "legacy" in (service.unavailable_reason() or "").lower()
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_successful_run_uses_worker_thread_and_emits_result(qapp):
    model = ScriptedModel([ModelResponse(text="Hello from the agent.")])
    service = make_service(model)
    receiver = ResultReceiver()
    service.run_finished.connect(receiver.on_result)
    main_thread = threading.get_ident()
    try:
        result = await service.run("hello")

        assert isinstance(result, AgentRunResult)
        assert result.state is AgentState.COMPLETED
        assert result.final_text == "Hello from the agent."
        assert model.thread_ids
        assert model.thread_ids[0] != main_thread
        assert service.is_running is False
        qapp.processEvents()
        assert receiver.results == [result]
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_while_agent_is_blocked(qapp):
    model = BlockingModel()
    service = make_service(model)
    try:
        run_task = asyncio.create_task(service.run("first"))
        await wait_until(model.started.is_set)

        # The agent's model call is blocked in a worker thread; the asyncio
        # event loop must still be able to schedule unrelated callbacks.
        tick = asyncio.Event()
        asyncio.get_running_loop().call_soon(tick.set)
        await asyncio.wait_for(tick.wait(), timeout=1.0)
        assert run_task.done() is False

        model.release.set()
        result = await asyncio.wait_for(run_task, timeout=3.0)
        assert result.state is AgentState.COMPLETED
    finally:
        model.release.set()
        service.shutdown()


@pytest.mark.asyncio
async def test_duplicate_run_is_rejected_while_first_is_active(qapp):
    model = BlockingModel()
    service = make_service(model)
    try:
        first = asyncio.create_task(service.run("first"))
        await wait_until(model.started.is_set)

        assert service.is_running is True
        with pytest.raises(AgentServiceBusyError):
            await service.run("second")

        model.release.set()
        result = await asyncio.wait_for(first, timeout=3.0)
        assert result.state is AgentState.COMPLETED
        assert service.is_running is False
    finally:
        model.release.set()
        service.shutdown()


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_confirmation(qapp):
    task_service = FakeTaskService()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(name="create_task", arguments={"title": "Review"}),)
            )
        ]
    )
    service = make_service(model, task_service=task_service)
    try:
        run_task = asyncio.create_task(service.run("create a task"))
        await wait_until(lambda: service.confirmation_bridge.pending_count == 1)

        assert service.cancel("Cancelled by test") is True
        result = await asyncio.wait_for(run_task, timeout=3.0)

        assert result.state is AgentState.CANCELLED
        assert task_service.created == []
        assert service.is_running is False
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_cancellation_while_a_tool_is_executing(qapp):
    task_service = FakeTaskService()
    task_service.block_get = True
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(name="get_task", arguments={"task_id": 1}),)
            )
        ]
    )
    service = make_service(model, task_service=task_service)
    try:
        run_task = asyncio.create_task(service.run("look up task 1"))
        await wait_until(task_service.get_started.is_set)

        assert service.cancel("Cancelled by test") is True
        result = await asyncio.wait_for(run_task, timeout=3.0)

        assert result.state is AgentState.CANCELLED
        assert result.tool_results
        assert result.tool_results[-1].code == "cancelled"
        assert service.is_running is False
    finally:
        task_service.get_release.set()
        service.shutdown()


@pytest.mark.asyncio
async def test_model_error_becomes_failed_result_without_false_success(qapp):
    model = ScriptedModel([RuntimeError("boom api_key=super-secret")])
    service = make_service(model)
    try:
        result = await service.run("hello")

        assert result.state is AgentState.FAILED
        assert result.error_code == "model_error"
        assert "super-secret" not in (result.error or "")
        assert "REDACTED" in (result.error or "")
        assert result.success is False
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_unknown_tool_and_invalid_arguments_are_structured_failures(qapp):
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=(ToolCall(name="not_a_tool", arguments={}),)),
            ModelResponse(
                tool_calls=(ToolCall(name="get_task", arguments={"task_id": "bad"}),)
            ),
            ModelResponse(text="I could not complete those tool calls."),
        ]
    )
    service = make_service(model)
    try:
        result = await service.run("do something")

        assert result.state is AgentState.COMPLETED
        assert result.final_text == "I could not complete those tool calls."
        assert [item.code for item in result.tool_results] == [
            "not_found",
            "validation_error",
        ]
        assert all(not item.success for item in result.tool_results)
        assert len(result.failed_tool_results) == 2
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_tool_exception_is_reported_as_failure_not_success(qapp):
    task_service = FakeTaskService()
    task_service.get_error = RuntimeError("database failure")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(name="get_task", arguments={"task_id": 1}),)
            ),
            ModelResponse(text="The lookup failed."),
        ]
    )
    service = make_service(model, task_service=task_service)
    try:
        result = await service.run("look up task 1")

        assert result.state is AgentState.COMPLETED
        assert result.tool_results[0].success is False
        assert result.tool_results[0].code == "service_error"
        assert result.failed_tool_results
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_confirmation_approved_runs_the_real_tool(qapp):
    task_service = FakeTaskService()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(name="create_task", arguments={"title": "Review"}),)
            ),
            ModelResponse(text="The task was created."),
        ]
    )
    service = make_service(model, task_service=task_service)
    receiver = ConfirmationReceiver(service, decision=True)
    service.confirmation_requested.connect(receiver.on_prompt)
    try:
        run_task = asyncio.create_task(service.run("create a task"))
        await pump_until(qapp, lambda: len(receiver.prompts) == 1)
        result = await asyncio.wait_for(run_task, timeout=3.0)

        assert result.state is AgentState.COMPLETED
        assert result.tool_results[0].success is True
        assert len(task_service.created) == 1
        assert task_service.created[0]["title"] == "Review"
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_confirmation_denied_does_not_run_the_tool(qapp):
    task_service = FakeTaskService()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(name="create_task", arguments={"title": "Review"}),)
            )
        ]
    )
    service = make_service(model, task_service=task_service)
    receiver = ConfirmationReceiver(service, decision=False)
    service.confirmation_requested.connect(receiver.on_prompt)
    try:
        run_task = asyncio.create_task(service.run("create a task"))
        await pump_until(qapp, lambda: len(receiver.prompts) == 1)
        result = await asyncio.wait_for(run_task, timeout=3.0)

        assert result.state is AgentState.FAILED
        assert result.error_code == "confirmation_denied"
        assert result.tool_results[0].code == "confirmation_denied"
        assert task_service.created == []
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_confirmation_timeout_does_not_run_the_tool(qapp):
    task_service = FakeTaskService()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(name="create_task", arguments={"title": "Review"}),)
            )
        ]
    )
    service = make_service(
        model, task_service=task_service, confirmation_timeout=1.0
    )
    try:
        started = time.monotonic()
        result = await asyncio.wait_for(service.run("create a task"), timeout=4.0)
        elapsed = time.monotonic() - started

        assert elapsed >= 0.9
        assert result.state is AgentState.FAILED
        assert result.error_code == "confirmation_denied"
        assert task_service.created == []
    finally:
        service.shutdown()
