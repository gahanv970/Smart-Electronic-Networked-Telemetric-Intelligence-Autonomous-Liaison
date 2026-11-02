from unittest.mock import MagicMock, patch
import pytest

from gemini_api import GeminiAIProvider
from whatsapp_service import WhatsAppNotificationListener, WhatsAppService


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


def test_stop_monitoring(listener):
    listener.stop_monitoring()
    assert listener.is_monitoring is False
