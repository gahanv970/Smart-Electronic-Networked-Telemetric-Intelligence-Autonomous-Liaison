"""GUI-level tests for voice -> command -> agent/fallback routing.

These tests replace the old intent-dispatch workflow tests.  The legacy
``understand_intent`` path must not run for the same command, and a fallback is
allowed to chat only; it must never execute an action.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
import qasync

import Jarvis_UI
from engine.agent_loop import AgentEvent, AgentRunResult, AgentState
from engine.qt_confirmation_bridge import ConfirmationPrompt
from engine.tool_registry import (
    ConfirmationRequest,
    Permission,
    ToolResult,
)
from voice_service import MockVoiceProvider


@pytest.fixture(scope="session")
def init_qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def qevent_loop(init_qapp):
    loop = qasync.QEventLoop(init_qapp)
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


class FakeAgentService:
    """Small test double; the real service is covered in test_agent_service.py."""

    def __init__(
        self,
        result=None,
        *,
        available=True,
        reason=None,
        error=None,
    ) -> None:
        self.result = result
        self.available = available
        self.reason = reason
        self.error = error
        self.calls = []
        self.resolved = []
        self.cancelled = []
        self.shutdown_called = False

    def is_available(self):
        return self.available

    def unavailable_reason(self):
        return self.reason

    async def run(self, user_message, *, conversation_history=None):
        self.calls.append((user_message, conversation_history))
        if self.error is not None:
            raise self.error
        return self.result

    def resolve_confirmation(self, request_id, approved):
        self.resolved.append((request_id, approved))
        return True

    def cancel(self, reason="Cancelled by user"):
        self.cancelled.append(reason)
        return True

    def shutdown(self):
        self.shutdown_called = True


def prepare_window(agent_service):
    window = Jarvis_UI.JarvisMainWindow()
    window.voice_manager._provider = MockVoiceProvider()
    window.ai_provider.is_configured = MagicMock(return_value=True)
    window.agent_service = agent_service
    return window


def make_voice_bytes():
    return b"x" * 500


def test_voice_workflow_routes_transcript_to_agent(qevent_loop):
    result = AgentRunResult(
        state=AgentState.COMPLETED,
        final_text="I can help with that.",
        tool_results=[
            ToolResult(
                success=True,
                tool_name="get_system_status",
                message="System status retrieved.",
                data={"os": "Windows"},
            )
        ],
    )
    fake_agent = FakeAgentService(result)
    window = prepare_window(fake_agent)
    window.mic_service.stop_recording = MagicMock(return_value=make_voice_bytes())
    window.mic_service.is_recording = MagicMock(return_value=True)

    with patch("Jarvis_UI.transcribe_audio_bytes", return_value="Status report"), \
         patch.object(
             window.ai_provider,
             "understand_intent",
             side_effect=AssertionError("legacy intent dispatch must not run"),
         ):
        qevent_loop.run_until_complete(
            window._toggle_microphone_recording.__wrapped__(window)
        )

    display = window.conversation_display.toPlainText()
    assert "Status report" in display
    assert "I can help with that." in display
    assert "get_system_status" in display
    assert fake_agent.calls
    assert fake_agent.calls[0][0] == "Status report"
    assert len(window.voice_manager._provider.spoken_texts) > 0
    window.close()


def test_agent_failure_is_displayed_honestly(qevent_loop):
    result = AgentRunResult(
        state=AgentState.FAILED,
        message="User denied confirmation for 'create_task'.",
        error="User denied confirmation for 'create_task'.",
        error_code="confirmation_denied",
        tool_results=[
            ToolResult.failure(
                "create_task",
                "confirmation_denied",
                "User denied confirmation for 'create_task'.",
            )
        ],
    )
    fake_agent = FakeAgentService(result)
    window = prepare_window(fake_agent)
    window.mic_service.stop_recording = MagicMock(return_value=make_voice_bytes())
    window.mic_service.is_recording = MagicMock(return_value=True)

    with patch("Jarvis_UI.transcribe_audio_bytes", return_value="Create a task"), \
         patch.object(
             window.ai_provider,
             "understand_intent",
             side_effect=AssertionError("legacy intent dispatch must not run"),
         ):
        qevent_loop.run_until_complete(
            window._toggle_microphone_recording.__wrapped__(window)
        )

    display = window.conversation_display.toPlainText()
    assert "Agent run failed [confirmation_denied]" in display
    assert "User denied confirmation" in display
    assert "Agent Error" in window.ai_status_label.text()
    window.close()


def test_completed_agent_with_failed_tool_is_not_presented_as_success(qevent_loop):
    result = AgentRunResult(
        state=AgentState.COMPLETED,
        final_text="The task was created successfully.",
        tool_results=[
            ToolResult.failure(
                "create_task",
                "service_error",
                "The task service failed to create the task.",
            )
        ],
    )
    fake_agent = FakeAgentService(result)
    window = prepare_window(fake_agent)
    window.command_input.setText("Create a task")

    qevent_loop.run_until_complete(window._send_command.__wrapped__(window))

    display = window.conversation_display.toPlainText()
    assert "Unverified agent summary" in display
    assert "No success is confirmed" in display
    assert "Completed with tool failures" in window.agent_progress_label.text()
    assert window.ai_status_label.text() == "AI Engine: Agent Ready"
    window.close()


def test_chat_only_fallback_never_executes_actions(qevent_loop):
    fake_agent = FakeAgentService(
        available=False, reason="The modern google-genai SDK is not available."
    )
    window = prepare_window(fake_agent)
    window.mic_service.stop_recording = MagicMock(return_value=make_voice_bytes())
    window.mic_service.is_recording = MagicMock(return_value=True)
    window.ai_provider.send_message = MagicMock(return_value="Chat-only answer.")

    with patch("Jarvis_UI.transcribe_audio_bytes", return_value="Open Notepad"), \
         patch.object(
             window.ai_provider,
             "understand_intent",
             side_effect=AssertionError("legacy intent dispatch must not run"),
         ), \
         patch.object(
             window.task_service,
             "create_task",
             side_effect=AssertionError("fallback must not create tasks"),
         ), \
         patch.object(
             window.automation_service,
             "launch_app",
             side_effect=AssertionError("fallback must not launch apps"),
         ):
        qevent_loop.run_until_complete(
            window._toggle_microphone_recording.__wrapped__(window)
        )

    display = window.conversation_display.toPlainText()
    assert "Chat-only answer." in display
    assert "Agent action engine unavailable" in display
    assert window.agent_progress_label.text() == "Agent: Chat-only fallback"
    assert fake_agent.calls == []
    assert window.ai_provider.send_message.called
    window.close()


def test_tts_error_does_not_hide_the_agent_response(qevent_loop):
    result = AgentRunResult(
        state=AgentState.COMPLETED,
        final_text="S.E.N.T.I.A.L. is functioning normally.",
    )
    fake_agent = FakeAgentService(result)
    window = prepare_window(fake_agent)
    failing_voice = MockVoiceProvider()
    failing_voice.speak = MagicMock(side_effect=RuntimeError("TTS Speech Error"))
    window.voice_manager._provider = failing_voice
    window.mic_service.stop_recording = MagicMock(return_value=make_voice_bytes())
    window.mic_service.is_recording = MagicMock(return_value=True)

    with patch("Jarvis_UI.transcribe_audio_bytes", return_value="Status report"), \
         patch.object(
             window.ai_provider,
             "understand_intent",
             side_effect=AssertionError("legacy intent dispatch must not run"),
         ):
        qevent_loop.run_until_complete(
            window._toggle_microphone_recording.__wrapped__(window)
        )

    display = window.conversation_display.toPlainText()
    assert "S.E.N.T.I.A.L. is functioning normally." in display
    window.close()


def test_agent_progress_and_cancel_button_reflect_real_state(qevent_loop):
    fake_agent = FakeAgentService(AgentRunResult(state=AgentState.COMPLETED))
    window = prepare_window(fake_agent)
    window._agent_run_active = True
    window._set_busy(True)

    assert window.cancel_button.isEnabled() is True

    window._on_agent_event(
        AgentEvent(
            state=AgentState.WAITING_FOR_CONFIRMATION,
            kind="confirmation_requested",
            message="Waiting for application confirmation of 'create_task'.",
            tool_name="create_task",
        )
    )
    assert "waiting for your confirmation" in window.agent_progress_label.text()

    window._on_agent_event(
        AgentEvent(
            state=AgentState.FAILED,
            kind="failed",
            message="The workflow failed.",
        )
    )
    assert "failed" in window.agent_progress_label.text().lower()

    window._agent_run_active = False
    window._set_busy(False)
    assert window.cancel_button.isEnabled() is False
    window.close()


def test_gui_confirmation_dialog_resolves_approval_and_denial(qevent_loop):
    fake_agent = FakeAgentService(AgentRunResult(state=AgentState.COMPLETED))
    window = prepare_window(fake_agent)
    window._agent_run_active = True

    request = ConfirmationRequest(
        tool_name="create_task",
        description="Create one task in the persistent task list.",
        permission=Permission.CONFIRMATION_REQUIRED,
        arguments={"title": "Review Phase 3"},
    )
    now = time.monotonic()
    prompt = ConfirmationPrompt(
        request_id="request-1",
        request=request,
        created_at=now,
        expires_at=now + 60.0,
    )

    window._on_agent_confirmation_requested(prompt)
    box = window._active_confirmation_box
    assert box is not None
    assert window._active_confirmation_id == "request-1"

    window._on_confirmation_dialog_finished(
        "request-1", int(QMessageBox.Yes), box
    )

    assert fake_agent.resolved == [("request-1", True)]
    assert window._active_confirmation_box is None
    assert window._active_confirmation_id is None

    # A prompt that arrives after the run is no longer active must be denied.
    window._agent_run_active = False
    late_prompt = ConfirmationPrompt(
        request_id="request-2",
        request=request,
        created_at=now,
        expires_at=now + 60.0,
    )
    window._on_agent_confirmation_requested(late_prompt)
    assert fake_agent.resolved[-1] == ("request-2", False)
    assert window._active_confirmation_box is None
    window.close()
