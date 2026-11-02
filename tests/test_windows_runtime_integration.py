from unittest.mock import MagicMock, patch
import pytest

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication

from Jarvis_UI import JarvisMainWindow
from gmail_service import GmailService
from microphone_service import MicrophoneService
from voice_service import (
    DisabledVoiceProvider,
    ElevenLabsVoiceProvider,
    MockVoiceProvider,
    VoiceManager,
    WindowsSapiVoiceProvider,
)
from whatsapp_service import WhatsAppNotificationListener, WhatsAppService


@pytest.fixture
def app():
    qapp = QApplication.instance()
    if not qapp:
        qapp = QApplication([])
    return qapp


def test_microphone_device_detection(app):
    mic_svc = MicrophoneService()
    with patch("PySide6.QtMultimedia.QMediaDevices.audioInputs", return_value=[]):
        has_mic = mic_svc.has_microphone()
        assert isinstance(has_mic, bool)
        assert has_mic is False


def test_voice_providers_and_manager(app):
    mgr = VoiceManager()
    assert isinstance(mgr.get_status(), str)

    # Test Disabled
    mgr.set_provider_type("disabled")
    assert mgr.is_available() is False

    # Test Mock
    mgr.set_provider_type("mock")
    assert mgr.is_available() is True
    mgr.speak("Hello Windows 11")
    mgr.stop()

    # Test SAPI on non-Windows fallback
    with patch("sys.platform", "linux"):
        sapi = WindowsSapiVoiceProvider()
        assert sapi.is_available() is False
        assert "Windows" in sapi.get_status()


def test_whatsapp_notification_listener_runtime_check():
    wa_svc = WhatsAppService()
    listener = WhatsAppNotificationListener(wa_svc)

    with patch("sys.platform", "linux"):
        supported, status = listener.check_notification_support()
        assert supported is False
        assert "Manual Import" in status


def test_gmail_oauth_runtime_unauthenticated_state():
    gmail_svc = GmailService()
    assert gmail_svc.is_authenticated() is False
    assert gmail_svc.get_user_email() is None


def test_jarvis_main_window_startup_and_navigation(app):
    win = JarvisMainWindow()
    assert win.windowTitle().startswith("S.E.N.T.I.A.L.")

    # Verify all 10 pages exist in stacked widget (Dashboard + 9 feature pages)
    assert win.stacked_widget.count() == 10

    # Test page navigation
    for page_idx in range(10):
        win.stacked_widget.setCurrentIndex(page_idx)
        assert win.stacked_widget.currentIndex() == page_idx

    # Close window cleanly
    from PySide6.QtGui import QCloseEvent
    close_evt = QCloseEvent()
    win.closeEvent(close_evt)
