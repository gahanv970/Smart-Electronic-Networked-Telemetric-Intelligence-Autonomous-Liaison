from io import BytesIO
from unittest.mock import MagicMock, patch
import urllib.request
import pytest

from browser_service import BrowserAutomationService


@pytest.fixture
def browser_svc():
    svc = BrowserAutomationService()
    svc.set_browser_automation_enabled(True)
    return svc


def test_url_security_validation(browser_svc):
    # Valid HTTPS URL
    valid, clean_url, msg = browser_svc.validate_url("https://example.com/test")
    assert valid is True
    assert clean_url == "https://example.com/test"

    # Prepend https if no scheme
    valid_auto, clean_auto, _ = browser_svc.validate_url("github.com/openai")
    assert valid_auto is True
    assert clean_auto == "https://github.com/openai"

    # Reject dangerous schemes
    invalid_js, _, err_js = browser_svc.validate_url("javascript:alert('xss')")
    assert invalid_js is False
    assert "strictly prohibited" in err_js or "prohibited" in err_js

    invalid_file, _, err_file = browser_svc.validate_url("file:///C:/Windows/System32/config")
    assert invalid_file is False
    assert "prohibited" in err_file


def test_navigate_url_and_tabs(browser_svc):
    with patch("webbrowser.open") as mock_open:
        res = browser_svc.navigate_url("https://python.org", user_confirmed=True)
        assert res.success is True
        mock_open.assert_called_once_with("https://python.org")
        assert res.data["navigation_verified"] is False
        assert "could not be verified" in res.message

        tabs = browser_svc.get_open_tabs()
        assert len(tabs) == 1
        assert tabs[0].url == "https://python.org"


def test_navigate_url_reports_launch_rejection(browser_svc):
    with patch("webbrowser.open", return_value=False):
        res = browser_svc.navigate_url("https://python.org", user_confirmed=True)
    assert res.success is False
    assert res.data["launch_accepted"] is False
    assert res.data["navigation_verified"] is False
    assert browser_svc.get_open_tabs() == []


def test_search_web(browser_svc):
    with patch("webbrowser.open") as mock_open:
        res = browser_svc.search_web("PySide6 documentation", user_confirmed=True)
        assert res.success is True
        mock_open.assert_called_once()
        called_url = mock_open.call_args[0][0]
        assert "google.com/search?q=PySide6" in called_url


def test_extract_page_text_mocked_html(browser_svc):
    mock_html = """
    <html>
      <head><title>Test Page</title><style>body { color: red; }</style></head>
      <body>
        <script>var x = 1;</script>
        <h1>Welcome to SENTIAL Test Page</h1>
        <p>This is a safe readable paragraph on the webpage.</p>
      </body>
    </html>
    """
    mock_response = MagicMock()
    mock_response.read.return_value = mock_html.encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response):
        res = browser_svc.extract_page_text("https://example.com/page")
        assert res.success is True
        extracted = res.data.get("extracted_text", "")
        assert "Welcome to SENTIAL Test Page" in extracted
        assert "This is a safe readable paragraph" in extracted
        assert "var x = 1" not in extracted
        assert "color: red" not in extracted


def test_master_browser_toggle(browser_svc):
    browser_svc.set_browser_automation_enabled(False)
    assert browser_svc.is_browser_automation_enabled() is False

    res = browser_svc.navigate_url("https://example.com", user_confirmed=True)
    assert res.success is False
    assert "disabled in settings" in res.message


def test_tab_lifecycle(browser_svc):
    tab1 = browser_svc.open_tab("https://site1.com", "Site 1")
    tab2 = browser_svc.open_tab("https://site2.com", "Site 2")

    assert len(browser_svc.get_open_tabs()) == 2
    assert tab2.active is True
    assert tab1.active is False

    switched = browser_svc.switch_tab(tab1.id)
    assert switched.active is True

    closed = browser_svc.close_tab(tab2.id)
    assert closed is True
    assert len(browser_svc.get_open_tabs()) == 1
