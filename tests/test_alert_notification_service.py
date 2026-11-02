from unittest.mock import patch
import pytest

from alert_service import AlertNotification, AlertNotificationService
from gmail_service import EmailSummary
from whatsapp_service import WhatsAppMessage


@pytest.fixture
def alert_svc(tmp_path):
    alerts_file = tmp_path / "alert_center.json"
    with patch("alert_service.ALERTS_FILE", alerts_file):
        svc = AlertNotificationService()
        yield svc


def test_whatsapp_alert_aggregation_and_priority_lock(alert_svc):
    wa_msg = WhatsAppMessage(
        id="wa_101",
        sender="Boss",
        message_text="Please send quarterly report immediately.",
        timestamp="2025-01-01 10:00:00",
        summary="Urgent report request from Boss.",
        urgency="low",  # Raw AI urgency rated low
    )

    # Contact priority lock should override urgency to high/critical
    alert = alert_svc.add_whatsapp_alert(wa_msg, category="Work", contact_priority="CRITICAL")
    assert alert.id == "alert_wa_101"
    assert alert.source == "WhatsApp"
    assert alert.urgency == "critical"
    assert alert.category == "Work"
    assert alert.is_read is False


def test_gmail_alert_aggregation(alert_svc):
    email = EmailSummary(
        id="msg_gmail_202",
        thread_id="th_202",
        sender="billing@service.com",
        subject="Invoice Overdue Notice",
        snippet="Your monthly account invoice is ready for review.",
        date="2025-01-01 11:00:00"
    )

    alert = alert_svc.add_gmail_alert(email, urgency="high", ai_summary="Invoice overdue notification.")
    assert alert.id == "alert_gmail_msg_gmail_202"
    assert alert.source == "Gmail"
    assert alert.urgency == "high"
    assert alert.category == "Email"


def test_sorting_and_ranking_precedence(alert_svc):
    wa_low = WhatsAppMessage(id="w_1", sender="Friend", message_text="Hi", timestamp="2025-01-01 12:00:00", urgency="low")
    wa_crit = WhatsAppMessage(id="w_2", sender="Family", message_text="Emergency", timestamp="2025-01-01 09:00:00", urgency="critical")

    alert_svc.add_whatsapp_alert(wa_low)
    alert_svc.add_whatsapp_alert(wa_crit, contact_priority="CRITICAL")

    alerts = alert_svc.get_alerts()
    assert len(alerts) == 2
    # Critical alert should rank first regardless of being earlier timestamp
    assert alerts[0].id == "alert_w_2"
    assert alerts[0].urgency == "critical"


def test_filtering_and_search(alert_svc):
    wa_msg = WhatsAppMessage(id="w_3", sender="Alice", message_text="Project update", timestamp="2025-01-01 13:00:00", urgency="normal")
    gmail_msg = EmailSummary(id="g_3", thread_id="t3", sender="bob@company.com", subject="Meeting Agenda", snippet="Notes", date="2025-01-01 14:00:00")

    alert_svc.add_whatsapp_alert(wa_msg, category="Work")
    alert_svc.add_gmail_alert(gmail_msg, urgency="high", ai_summary="Agenda notes")

    # Source filter
    wa_only = alert_svc.get_alerts(source_filter="WhatsApp")
    assert len(wa_only) == 1
    assert wa_only[0].source == "WhatsApp"

    # Search query
    search_res = alert_svc.get_alerts(search_query="Agenda")
    assert len(search_res) == 1
    assert search_res[0].source == "Gmail"


def test_mark_read_and_dismiss_controls(alert_svc):
    wa_msg = WhatsAppMessage(id="w_4", sender="DevOps", message_text="Alert", timestamp="2025-01-01 15:00:00")
    alert_svc.add_whatsapp_alert(wa_msg)

    assert alert_svc.get_unread_count() == 1

    # Mark as read
    alert_svc.mark_as_read("alert_w_4")
    assert alert_svc.get_unread_count() == 0

    # Dismiss alert
    alert_svc.dismiss_alert("alert_w_4")
    visible = alert_svc.get_alerts(include_dismissed=False)
    assert len(visible) == 0


def test_clear_history(alert_svc):
    wa_msg = WhatsAppMessage(id="w_5", sender="Test", message_text="Test msg", timestamp="2025-01-01 16:00:00")
    alert_svc.add_whatsapp_alert(wa_msg)

    assert len(alert_svc.get_alerts()) == 1
    alert_svc.clear_history()
    assert len(alert_svc.get_alerts()) == 0
