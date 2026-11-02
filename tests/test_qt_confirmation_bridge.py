"""Offline tests for the Qt confirmation bridge.

No Gemini, network, filesystem or real dialog is used.  A ``QApplication`` is
created so Qt's queued cross-thread signal delivery can be exercised exactly as
it is in the application.
"""

from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtWidgets import QApplication

from engine.qt_confirmation_bridge import QtConfirmationBridge
from engine.tool_registry import ConfirmationRequest, Permission


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def make_request(title: str = "Review Phase 3") -> ConfirmationRequest:
    return ConfirmationRequest(
        tool_name="create_task",
        description="Create one task in the persistent task list.",
        permission=Permission.CONFIRMATION_REQUIRED,
        arguments={"title": title},
    )


class PromptReceiver(QObject):
    """GUI-thread receiver that records prompts and optionally resolves them."""

    def __init__(self, bridge: QtConfirmationBridge, decision=None) -> None:
        super().__init__()
        self.bridge = bridge
        self.decision = decision
        self.prompts = []
        self.closed = []

    @Slot(object)
    def on_prompt(self, prompt) -> None:
        self.prompts.append(prompt)
        if self.decision is not None:
            self.bridge.resolve(prompt.request_id, self.decision)

    @Slot(str, str)
    def on_closed(self, request_id: str, reason: str) -> None:
        self.closed.append((request_id, reason))


def start_approval(bridge: QtConfirmationBridge, title: str = "Review Phase 3"):
    result = []
    thread = threading.Thread(
        target=lambda: result.append(bridge.approve(make_request(title))),
        daemon=True,
    )
    thread.start()
    return thread, result


def pump_until(qapp, predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    return predicate()


def test_approved_prompt_is_delivered_to_gui_and_returns_true(qapp):
    bridge = QtConfirmationBridge(timeout_seconds=2.0)
    receiver = PromptReceiver(bridge, decision=True)
    bridge.confirmation_requested.connect(receiver.on_prompt)
    bridge.confirmation_closed.connect(receiver.on_closed, Qt.DirectConnection)

    thread, result = start_approval(bridge)

    assert pump_until(qapp, lambda: bool(result))
    thread.join(2.0)

    assert result == [True]
    assert len(receiver.prompts) == 1
    assert receiver.prompts[0].request.tool_name == "create_task"
    assert receiver.prompts[0].request.arguments["title"] == "Review Phase 3"
    assert bridge.pending_count == 0
    assert receiver.closed[-1][1] == "approved"


def test_denied_prompt_returns_false_and_does_not_authorize(qapp):
    bridge = QtConfirmationBridge(timeout_seconds=2.0)
    receiver = PromptReceiver(bridge, decision=False)
    bridge.confirmation_requested.connect(receiver.on_prompt)
    bridge.confirmation_closed.connect(receiver.on_closed, Qt.DirectConnection)

    thread, result = start_approval(bridge)

    assert pump_until(qapp, lambda: bool(result))
    thread.join(2.0)

    assert result == [False]
    assert bridge.pending_count == 0
    assert receiver.closed[-1][1] == "denied"


def test_timeout_returns_false_and_cleans_pending_prompt(qapp):
    bridge = QtConfirmationBridge(timeout_seconds=0.15)
    receiver = PromptReceiver(bridge, decision=None)
    bridge.confirmation_requested.connect(receiver.on_prompt)
    bridge.confirmation_closed.connect(receiver.on_closed, Qt.DirectConnection)

    thread, result = start_approval(bridge)
    started = time.monotonic()

    assert pump_until(qapp, lambda: bool(result), timeout=2.0)
    thread.join(2.0)
    elapsed = time.monotonic() - started

    assert result == [False]
    assert elapsed >= 0.10
    assert bridge.pending_count == 0
    assert any(reason == "timeout" for _, reason in receiver.closed)


def test_late_or_replayed_resolution_cannot_approve_anything(qapp):
    bridge = QtConfirmationBridge(timeout_seconds=2.0)
    receiver = PromptReceiver(bridge, decision=None)
    bridge.confirmation_requested.connect(receiver.on_prompt)

    thread, result = start_approval(bridge)
    assert pump_until(qapp, lambda: len(receiver.prompts) == 1)
    request_id = receiver.prompts[0].request_id

    assert bridge.resolve("unknown-request-id", True) is False
    assert bridge.pending_count == 1

    assert bridge.resolve(request_id, False) is True
    assert pump_until(qapp, lambda: bool(result))
    thread.join(2.0)

    # A replay of the same id must not resolve anything or mint approval.
    assert bridge.resolve(request_id, True) is False
    assert result == [False]
    assert bridge.pending_count == 0


def test_cancellation_checker_stops_waiting_without_approval(qapp):
    bridge = QtConfirmationBridge(timeout_seconds=5.0)
    cancelled = threading.Event()
    bridge.set_cancellation_checker(cancelled.is_set)
    receiver = PromptReceiver(bridge, decision=None)
    bridge.confirmation_requested.connect(receiver.on_prompt)
    bridge.confirmation_closed.connect(receiver.on_closed, Qt.DirectConnection)

    thread, result = start_approval(bridge)
    assert pump_until(qapp, lambda: len(receiver.prompts) == 1)

    cancelled.set()
    assert pump_until(qapp, lambda: bool(result), timeout=2.0)
    thread.join(2.0)

    assert result == [False]
    assert bridge.pending_count == 0
    assert any(reason == "cancelled" for _, reason in receiver.closed)


def test_closed_bridge_fails_closed_for_new_and_pending_requests(qapp):
    bridge = QtConfirmationBridge(timeout_seconds=2.0)
    bridge.close()

    assert bridge.is_closed is True
    assert bridge.approve(make_request()) is False
    assert bridge.pending_count == 0

    pending_bridge = QtConfirmationBridge(timeout_seconds=5.0)
    receiver = PromptReceiver(pending_bridge, decision=None)
    pending_bridge.confirmation_requested.connect(receiver.on_prompt)
    pending_bridge.confirmation_closed.connect(
        receiver.on_closed, Qt.DirectConnection
    )

    thread, result = start_approval(pending_bridge)
    assert pump_until(qapp, lambda: len(receiver.prompts) == 1)

    pending_bridge.close()
    assert pump_until(qapp, lambda: bool(result), timeout=2.0)
    thread.join(2.0)

    assert result == [False]
    assert pending_bridge.pending_count == 0
    assert any("closed" in reason for _, reason in receiver.closed)


def test_invalid_request_and_checker_are_rejected():
    bridge = QtConfirmationBridge(timeout_seconds=1.0)
    try:
        assert bridge.approve({"tool_name": "create_task"}) is False
        with pytest.raises(TypeError):
            bridge.set_cancellation_checker("not-callable")
    finally:
        bridge.close()
