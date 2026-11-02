from unittest.mock import MagicMock, patch
import pytest

from automation_service import WindowsAutomationService


@pytest.fixture
def auto_service():
    svc = WindowsAutomationService()
    svc.set_automation_enabled(True)
    return svc


def test_master_automation_disable_toggle(auto_service):
    auto_service.set_automation_enabled(False)
    assert auto_service.is_automation_enabled() is False

    res = auto_service.launch_app("notepad", user_confirmed=True)
    assert res.success is False
    assert "disabled in settings" in res.message

    res_url = auto_service.open_url("https://example.com", user_confirmed=True)
    assert res_url.success is False
    assert "disabled in settings" in res_url.message


def test_open_path_validation_and_traversal_protection(auto_service, tmp_path):
    test_file = tmp_path / "sample.txt"
    test_file.write_text("hello world")

    # Path traversal attempt
    res = auto_service.open_path("../../../etc/passwd", user_confirmed=True)
    assert res.success is False
    assert "Path traversal '..' is prohibited" in res.message

    # Non-existent path
    res_missing = auto_service.open_path(str(tmp_path / "missing.file"), user_confirmed=True)
    assert res_missing.success is False
    assert "Path not found" in res_missing.message

    # Restricted system path
    res_sys = auto_service.open_path("C:\\Windows\\System32\\config", user_confirmed=True)
    assert res_sys.success is False
    assert "Access to critical system directories is restricted" in res_sys.message or "Path not found" in res_sys.message

    # Successful mocked opening
    with patch("os.startfile", create=True) as mock_startfile, \
         patch("subprocess.Popen") as mock_popen:
        res_ok = auto_service.open_path(str(test_file), user_confirmed=True)
        assert res_ok.success is True
        assert str(test_file.resolve()) in res_ok.message


def test_web_search(auto_service):
    with patch("webbrowser.open") as mock_web_open:
        res = auto_service.web_search("PySide6 Python desktop assistant", user_confirmed=True)
        assert res.success is True
        mock_web_open.assert_called_once()
        called_url = mock_web_open.call_args[0][0]
        assert "google.com/search?q=" in called_url
        assert "PySide6" in called_url


def test_window_management_validation(auto_service):
    # Unapproved app
    res = auto_service.window_management("minimize", "unapproved_app_id", user_confirmed=True)
    assert res.success is False
    assert "not in approved allowlist" in res.message

    # Invalid action
    res_invalid_act = auto_service.window_management("destroy_all", "notepad", user_confirmed=True)
    assert res_invalid_act.success is False
    assert "Invalid window action" in res_invalid_act.message

    # Confirmation required
    res_unconfirmed = auto_service.window_management("minimize", "notepad", user_confirmed=False)
    assert res_unconfirmed.success is False
    assert "requires explicit confirmation" in res_unconfirmed.message

    # Confirmed action with no matching window must not claim success.
    with patch(
        "automation_service.find_windows_for_executable", return_value=[]
    ):
        res_no_window = auto_service.window_management(
            "minimize", "notepad", user_confirmed=True
        )
    assert res_no_window.success is False
    assert "No visible Notepad window" in res_no_window.message
    assert res_no_window.data["verified"] is False

    # Confirmed action against a real window handle (Win32 calls are mocked).
    with patch(
        "automation_service.find_windows_for_executable",
        return_value=[{"handle": 123, "process_id": 7, "title": "Untitled - Notepad"}],
    ), patch(
        "automation_service.apply_verified_window_action", return_value=True
    ) as apply_action:
        res_ok = auto_service.window_management(
            "minimize", "notepad", user_confirmed=True
        )
    assert res_ok.success is True
    assert "completed and verified" in res_ok.message
    assert res_ok.data["verified"] is True
    apply_action.assert_called_once_with(123, "minimize")

    # A requested action whose state cannot be verified is a failure.
    with patch(
        "automation_service.find_windows_for_executable",
        return_value=[{"handle": 123, "process_id": 7, "title": "Untitled - Notepad"}],
    ), patch(
        "automation_service.apply_verified_window_action", return_value=False
    ):
        res_unverified = auto_service.window_management(
            "minimize", "notepad", user_confirmed=True
        )
    assert res_unverified.success is False
    assert "could not be verified" in res_unverified.message


def test_audit_log_tracking(auto_service):
    auto_service.web_search("test query", user_confirmed=True)
    logs = auto_service.get_audit_log()
    assert len(logs) >= 1
    assert logs[-1].action in ("web_search", "open_url")
