import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from gemini_api import GeminiAIProvider
from whatsapp_service import (
    WhatsAppNotificationListener,
    WhatsAppService,
    _get_current_notification_listener,
)


@pytest.fixture
def whatsapp_svc(tmp_path):
    contacts_file = tmp_path / "whatsapp_contacts.json"
    history_file = tmp_path / "whatsapp_history.json"
    with patch("whatsapp_service.CONTACTS_FILE", contacts_file), \
         patch("whatsapp_service.HISTORY_FILE", history_file):
        svc = WhatsAppService()
        yield svc


@pytest.fixture
def listener(whatsapp_svc):
    mock_ai = MagicMock(spec=GeminiAIProvider)
    mock_ai.is_configured.return_value = False
    return WhatsAppNotificationListener(whatsapp_service=whatsapp_svc, ai_provider=mock_ai)


def test_notification_filtering_app_id(listener):
    # Non-WhatsApp notification should be rejected
    msg = listener.process_raw_notification(
        app_id="com.slack.Slack",
        app_name="Slack",
        sender="Bob",
        body="Meeting in 5 mins"
    )
    assert msg is None

    # WhatsApp Desktop notification should be accepted
    msg = listener.process_raw_notification(
        app_id="5319275a.whatsappdesktop_cv1g1gvanyjgm!app",
        app_name="WhatsApp",
        sender="Manager",
        body="Please send the weekly update."
    )
    assert msg is not None
    assert msg.sender == "Manager"
    assert msg.message_text == "Please send the weekly update."


def test_missing_or_partial_fields(listener):
    # Empty body should be rejected
    msg = listener.process_raw_notification(
        app_id="WhatsApp",
        app_name="WhatsApp Desktop",
        sender="Alice",
        body=""
    )
    assert msg is None

    # Missing sender should default to 'WhatsApp Contact'
    msg = listener.process_raw_notification(
        app_id="WhatsApp",
        app_name="WhatsApp Desktop",
        sender="",
        body="Hey there!",
        timestamp="2025-01-01 12:00:00"
    )
    assert msg is not None
    assert msg.sender == "WhatsApp Contact"


def test_duplicate_notification_suppression(listener):
    # First notification should be processed
    msg1 = listener.process_raw_notification(
        app_id="WhatsApp",
        app_name="WhatsApp",
        sender="John",
        body="Same message content",
        timestamp="2025-01-01 10:00:00"
    )
    assert msg1 is not None

    # Exact duplicate should be ignored
    msg2 = listener.process_raw_notification(
        app_id="WhatsApp",
        app_name="WhatsApp",
        sender="John",
        body="Same message content",
        timestamp="2025-01-01 10:00:00"
    )
    assert msg2 is None


def test_listener_platform_fallback(listener):
    with patch("sys.platform", "linux"):
        supported, status_msg = listener.start_monitoring()
        assert supported is False
        assert "Manual Import" in status_msg
        assert listener.is_monitoring is False


def test_current_notification_listener_supports_winrt_current_property():
    current_listener = object()

    class WinrtListener:
        current = current_listener

    assert _get_current_notification_listener(WinrtListener) is current_listener


def test_current_notification_listener_supports_legacy_get_current():
    current_listener = object()

    class LegacyListener:
        @staticmethod
        def get_current():
            return current_listener

    assert _get_current_notification_listener(LegacyListener) is current_listener


@pytest.mark.asyncio
async def test_start_monitoring_starts_async_polling_and_stop_cancels_it(listener, monkeypatch):
    def report_supported():
        listener._winrt_listener = object()
        return (
            True,
            "Live Windows WhatsApp Desktop Notification Monitoring Active",
        )

    async def read_empty_notifications():
        return []

    monkeypatch.setattr(
        listener,
        "_read_current_notifications",
        read_empty_notifications,
    )
    monkeypatch.setattr(
        listener,
        "check_notification_support",
        report_supported,
    )

    supported, _ = listener.start_monitoring()
    assert supported is True
    assert listener._poll_task is not None
    polling_task = listener._poll_task
    await asyncio.sleep(0)

    listener.stop_monitoring()
    await asyncio.gather(polling_task, return_exceptions=True)
    assert listener._poll_task is None
    assert listener.is_monitoring is False


@pytest.mark.asyncio
async def test_notification_poll_processes_new_ids_only(listener, monkeypatch):
    received = []
    monkeypatch.setattr(
        listener,
        "_process_user_notification",
        received.append,
    )
    old_notification = type("Notification", (), {"id": 1})()
    new_notification = type("Notification", (), {"id": 2})()
    listener._seen_notification_ids = {1}

    async def read_notifications():
        return [old_notification, new_notification]

    monkeypatch.setattr(listener, "_read_current_notifications", read_notifications)
    listener.is_monitoring = True

    await listener._poll_notifications()

    assert received == [new_notification]
    assert listener._seen_notification_ids == {1, 2}


def test_process_user_notification_extracts_whatsapp_toast(listener):
    notifications = pytest.importorskip("winrt.windows.ui.notifications")
    binding = MagicMock()
    binding.get_text_elements.return_value = [
        SimpleNamespace(text="Alice"),
        SimpleNamespace(text="Hello from WhatsApp"),
    ]
    visual = MagicMock()
    visual.get_binding.return_value = binding
    app_info = SimpleNamespace(
        app_user_model_id="5319275a.whatsappdesktop_cv1g1gvanyjgm!app",
        display_info=SimpleNamespace(display_name="WhatsApp"),
    )
    toast = SimpleNamespace(
        app_info=app_info,
        notification=SimpleNamespace(visual=visual),
        creation_time=datetime(2026, 11, 2, 12, 0),
    )
    listener.whatsapp_service.import_and_analyze_message = MagicMock(
        return_value=SimpleNamespace(id="message-1")
    )

    listener._process_user_notification(toast)

    listener.whatsapp_service.import_and_analyze_message.assert_called_once_with(
        sender="Alice",
        message_text="Hello from WhatsApp",
        ai_provider=listener.ai_provider,
    )
    visual.get_binding.assert_called_once_with(
        notifications.KnownNotificationBindings.toast_generic
    )


def test_stop_monitoring(listener):
    listener.stop_monitoring()
    assert listener.is_monitoring is False
