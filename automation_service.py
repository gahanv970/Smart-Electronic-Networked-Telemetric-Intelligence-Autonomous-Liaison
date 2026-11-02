"""
Windows Automation Service for G-AI-ASSISTANT.
Provides a strictly controlled, allowlisted execution engine for desktop automation,
application launching, browser navigation, system telemetry, and validated workflows.
Guarantees NO arbitrary shell command execution.
"""

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime
import logging
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional
import urllib.parse
import webbrowser

from config import get_config_value, save_config

logger = logging.getLogger(__name__)


@dataclass
class AllowedApp:
    id: str
    display_name: str
    executable: str
    description: str
    category: str
    requires_confirmation: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ActionResult:
    success: bool
    action: str
    message: str
    data: Optional[Dict[str, Any]] = None
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Controlled Allowlist of Approved Windows Applications
APPROVED_APPS: Dict[str, AllowedApp] = {
    "notepad": AllowedApp(
        id="notepad",
        display_name="Notepad",
        executable="notepad.exe",
        description="Standard Windows text editor",
        category="productivity",
        requires_confirmation=False,
    ),
    "calculator": AllowedApp(
        id="calculator",
        display_name="Calculator",
        executable="calc.exe",
        description="Windows Calculator",
        category="utility",
        requires_confirmation=False,
    ),
    "explorer": AllowedApp(
        id="explorer",
        display_name="File Explorer",
        executable="explorer.exe",
        description="Windows File Explorer",
        category="system",
        requires_confirmation=False,
    ),
    "paint": AllowedApp(
        id="paint",
        display_name="Paint",
        executable="mspaint.exe",
        description="Windows Paint drawing tool",
        category="creativity",
        requires_confirmation=False,
    ),
    "taskmgr": AllowedApp(
        id="taskmgr",
        display_name="Task Manager",
        executable="taskmgr.exe",
        description="Windows Task Manager monitor",
        category="system",
        requires_confirmation=True,
    ),
    "cmd": AllowedApp(
        id="cmd",
        display_name="Command Prompt",
        executable="cmd.exe",
        description="Windows Command Prompt console",
        category="developer",
        requires_confirmation=True,
    ),
}

# Pre-registered Safe Multi-Step Workflows
PRESET_WORKFLOWS: Dict[str, Dict[str, Any]] = {
    "dev_workspace": {
        "name": "Developer Workspace",
        "description": "Opens File Explorer and Notepad for quick development session",
        "steps": [
            {"action": "launch_app", "app_id": "explorer"},
            {"action": "launch_app", "app_id": "notepad"},
        ],
        "requires_confirmation": False,
    },
    "quick_notes": {
        "name": "Quick Notes",
        "description": "Launches Notepad and opens Google Search in browser",
        "steps": [
            {"action": "launch_app", "app_id": "notepad"},
            {"action": "open_url", "url": "https://www.google.com"},
        ],
        "requires_confirmation": False,
    },
}


# ---------------------------------------------------------------------------
# Verified Win32 window operations
# ---------------------------------------------------------------------------

_SW_MAXIMIZE = 3
_SW_MINIMIZE = 6
_SW_RESTORE = 9
_SW_SHOW = 5
_GW_OWNER = 4
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_image_path(pid: int) -> str:
    """Return a process image path using only documented Win32 APIs."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.LPDWORD,
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_windows_for_executable(executable: str) -> List[Dict[str, Any]]:
    """Enumerate visible top-level windows owned by ``executable``.

    Matching is by process image name, not by a caller-supplied window title, so
    a workflow cannot target an unrelated window.  Returns an empty list when
    no such window exists; raises on non-Windows or Win32 enumeration errors.
    """
    if os.name != "nt":
        raise OSError("Window management is only supported on Windows.")

    target = os.path.basename((executable or "").strip()).lower()
    if not target:
        return []

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    found: List[Dict[str, Any]] = []

    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        # Exclude owned dialogs/popups; we only manipulate top-level app windows.
        if user32.GetWindow(hwnd, _GW_OWNER):
            return True
        title_length = user32.GetWindowTextLengthW(hwnd)
        if title_length <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        image_path = _process_image_path(pid.value)
        if os.path.basename(image_path).lower() != target:
            return True
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)
        found.append(
            {
                "handle": int(hwnd),
                "process_id": int(pid.value),
                "title": title_buffer.value,
            }
        )
        return True

    callback = enum_proc(_callback)
    if not user32.EnumWindows(callback, 0):
        error = ctypes.get_last_error()
        # EnumWindows returns FALSE only when the callback aborts; with a
        # callback that always returns True this indicates a Win32 failure.
        if error:
            raise OSError(error, "EnumWindows failed.")
    return found


def _wait_for_window_state(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def apply_verified_window_action(hwnd: int, action: str) -> bool:
    """Perform one window action and verify the resulting window state."""
    if os.name != "nt":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    handle = wintypes.HWND(hwnd)
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND

    if action == "minimize":
        user32.ShowWindow(handle, _SW_MINIMIZE)
        return _wait_for_window_state(lambda: bool(user32.IsIconic(handle)))

    if action == "maximize":
        user32.ShowWindow(handle, _SW_MAXIMIZE)
        return _wait_for_window_state(
            lambda: bool(user32.IsZoomed(handle)) and not bool(user32.IsIconic(handle))
        )

    if action == "restore":
        user32.ShowWindow(handle, _SW_RESTORE)
        return _wait_for_window_state(
            lambda: not bool(user32.IsIconic(handle))
            and not bool(user32.IsZoomed(handle))
        )

    if action == "focus":
        user32.ShowWindow(handle, _SW_RESTORE)
        user32.BringWindowToTop(handle)
        user32.SetForegroundWindow(handle)
        return _wait_for_window_state(
            lambda: int(user32.GetForegroundWindow() or 0) == int(hwnd)
        )

    return False


class WindowsAutomationService:
    """
    Dedicated safe Windows automation engine.
    Executes only allowlisted applications and validated actions.
    Separates AI intent from raw system execution.
    Guarantees NO arbitrary shell execution or unrestricted code evaluation.
    """

    def __init__(self):
        self._audit_log: List[ActionResult] = []

    def is_automation_enabled(self) -> bool:
        """Returns True if desktop automation is enabled in application settings."""
        return bool(get_config_value("desktop_automation_enabled", True))

    def set_automation_enabled(self, enabled: bool) -> None:
        """Toggles desktop automation master enable/disable setting."""
        save_config({"desktop_automation_enabled": bool(enabled)})
        logger.info(f"Desktop automation master setting updated: enabled={enabled}")

    def get_allowed_apps(self) -> List[AllowedApp]:
        """Returns the list of all registered allowed applications."""
        return list(APPROVED_APPS.values())

    def get_registered_workflows(self) -> Dict[str, Dict[str, Any]]:
        """Returns registered workflows."""
        return PRESET_WORKFLOWS

    def get_audit_log(self) -> List[ActionResult]:
        """Returns recent automation execution logs."""
        return list(self._audit_log)

    def _log_result(self, result: ActionResult) -> ActionResult:
        """Appends action result to audit log."""
        self._audit_log.append(result)
        if len(self._audit_log) > 100:
            self._audit_log.pop(0)
        return result

    # ------------------------------------------------------------------ Core Safe Actions

    def launch_app(
        self,
        app_id: str,
        user_confirmed: bool = False,
    ) -> ActionResult:
        """
        Launches an allowlisted application without shell interpretation.
        Checks confirmation if required by app or global settings.
        """
        if not self.is_automation_enabled():
            res = ActionResult(
                success=False,
                action="launch_app",
                message="Access Denied: Desktop automation is disabled in settings.",
            )
            return self._log_result(res)

        clean_id = (app_id or "").strip().lower()
        if clean_id not in APPROVED_APPS:
            res = ActionResult(
                success=False,
                action="launch_app",
                message=f"Access Denied: Application '{app_id}' is not in the approved allowlist.",
            )
            return self._log_result(res)

        app_info = APPROVED_APPS[clean_id]
        global_confirm_required = get_config_value("automation_require_confirmation", True)

        if (app_info.requires_confirmation or global_confirm_required) and not user_confirmed:
            res = ActionResult(
                success=False,
                action="launch_app",
                message=f"Action '{app_info.display_name}' requires explicit user confirmation.",
                data={"app_id": clean_id, "needs_confirmation": True},
            )
            return self._log_result(res)

        try:
            # Safe launch using direct executable invocation without shell=True
            subprocess.Popen([app_info.executable], shell=False)
            res = ActionResult(
                success=True,
                action="launch_app",
                message=f"Successfully launched {app_info.display_name}.",
                data={"app_id": clean_id, "executable": app_info.executable},
            )
            return self._log_result(res)
        except Exception as e:
            logger.error(f"Failed to launch application {clean_id}: {e}")
            res = ActionResult(
                success=False,
                action="launch_app",
                message=f"Failed to launch {app_info.display_name}: {e}",
            )
            return self._log_result(res)

    def open_url(
        self,
        url: str,
        user_confirmed: bool = False,
    ) -> ActionResult:
        """
        Opens a validated HTTP/HTTPS URL in the default browser.
        Rejects non-HTTP schemes (e.g. file:, javascript:, data:, cmd:) to prevent injection.
        """
        if not self.is_automation_enabled():
            res = ActionResult(
                success=False,
                action="open_url",
                message="Access Denied: Desktop automation is disabled in settings.",
            )
            return self._log_result(res)

        clean_url = (url or "").strip()
        if not clean_url:
            res = ActionResult(success=False, action="open_url", message="URL cannot be empty.")
            return self._log_result(res)

        # If an explicit scheme is present (e.g. javascript:, file:, data:), validate it before altering
        if ":" in clean_url:
            scheme_candidate = clean_url.split(":", 1)[0].lower()
            if scheme_candidate not in ("http", "https"):
                res = ActionResult(
                    success=False,
                    action="open_url",
                    message=f"Security violation: Scheme '{scheme_candidate}' is prohibited. Only HTTP/HTTPS allowed.",
                )
                return self._log_result(res)

        # Prepend https:// if no scheme provided
        if not clean_url.startswith(("http://", "https://")):
            clean_url = "https://" + clean_url

        try:
            parsed = urllib.parse.urlparse(clean_url)
            if parsed.scheme.lower() not in ("http", "https"):
                res = ActionResult(
                    success=False,
                    action="open_url",
                    message=f"Security violation: Scheme '{parsed.scheme}' is prohibited. Only HTTP/HTTPS allowed.",
                )
                return self._log_result(res)

            if not parsed.netloc:
                res = ActionResult(
                    success=False, action="open_url", message="Invalid URL: Domain cannot be empty."
                )
                return self._log_result(res)

            launch_accepted = bool(webbrowser.open(clean_url))
            if not launch_accepted:
                res = ActionResult(
                    success=False,
                    action="open_url",
                    message=(
                        "The operating system did not accept the browser launch "
                        f"request for {clean_url}; no page was opened."
                    ),
                    data={
                        "url": clean_url,
                        "url_validated": True,
                        "launch_requested": True,
                        "launch_accepted": False,
                        "navigation_verified": False,
                    },
                )
                return self._log_result(res)

            res = ActionResult(
                success=True,
                action="open_url",
                message=(
                    f"Browser launch requested for {clean_url}. "
                    "Page navigation could not be verified."
                ),
                data={
                    "url": clean_url,
                    "url_validated": True,
                    "launch_requested": True,
                    "launch_accepted": True,
                    "navigation_verified": False,
                },
            )
            return self._log_result(res)
        except Exception as e:
            logger.error(f"Error opening URL {clean_url}: {e}")
            res = ActionResult(
                success=False, action="open_url", message=f"Failed to open URL: {e}"
            )
            return self._log_result(res)

    def open_path(
        self,
        file_or_folder_path: str,
        user_confirmed: bool = False,
    ) -> ActionResult:
        """
        Safely opens a local file or folder using native OS opener after strict path traversal
        validation and root folder restriction checks.
        """
        if not self.is_automation_enabled():
            res = ActionResult(
                success=False,
                action="open_path",
                message="Access Denied: Desktop automation is disabled in settings.",
            )
            return self._log_result(res)

        raw_path = (file_or_folder_path or "").strip()
        if not raw_path:
            res = ActionResult(success=False, action="open_path", message="Path cannot be empty.")
            return self._log_result(res)

        # Block path traversal signatures
        if ".." in raw_path:
            res = ActionResult(
                success=False,
                action="open_path",
                message="Security violation: Path traversal '..' is prohibited.",
            )
            return self._log_result(res)

        try:
            resolved_path = Path(raw_path).resolve()
            if not resolved_path.exists():
                res = ActionResult(
                    success=False,
                    action="open_path",
                    message=f"Path not found: '{raw_path}' does not exist on the filesystem.",
                )
                return self._log_result(res)

            # Restrict opening sensitive system roots (e.g. C:\Windows\System32, /etc, /sys)
            path_str = str(resolved_path).lower()
            prohibited_roots = ["system32", "syswow64", "windows\\system", "/etc", "/sys", "/proc", "/dev"]
            if any(p in path_str for p in prohibited_roots):
                res = ActionResult(
                    success=False,
                    action="open_path",
                    message="Security violation: Access to critical system directories is restricted.",
                )
                return self._log_result(res)

            global_confirm = get_config_value("automation_require_confirmation", True)
            if global_confirm and not user_confirmed:
                res = ActionResult(
                    success=False,
                    action="open_path",
                    message=f"Opening path '{resolved_path.name}' requires explicit user confirmation.",
                    data={"path": str(resolved_path), "needs_confirmation": True},
                )
                return self._log_result(res)

            # Platform-dependent safe launch
            if platform.system() == "Windows":
                os.startfile(str(resolved_path))
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(resolved_path)], shell=False)
            else:
                subprocess.Popen(["xdg-open", str(resolved_path)], shell=False)

            res = ActionResult(
                success=True,
                action="open_path",
                message=f"Opened path: {resolved_path}",
                data={"path": str(resolved_path)},
            )
            return self._log_result(res)
        except Exception as e:
            logger.error(f"Error opening path '{raw_path}': {e}")
            res = ActionResult(
                success=False, action="open_path", message=f"Failed to open path: {e}"
            )
            return self._log_result(res)

    def web_search(
        self,
        query: str,
        user_confirmed: bool = False,
    ) -> ActionResult:
        """
        Executes a secure web search using default browser and HTTPS search engine.
        """
        clean_query = (query or "").strip()
        if not clean_query:
            res = ActionResult(success=False, action="web_search", message="Search query cannot be empty.")
            return self._log_result(res)

        encoded_q = urllib.parse.quote_plus(clean_query)
        search_url = f"https://www.google.com/search?q={encoded_q}"
        return self.open_url(search_url, user_confirmed=user_confirmed)

    def window_management(
        self,
        action: str,
        app_id: str,
        user_confirmed: bool = False,
    ) -> ActionResult:
        """
        Manages supported application windows (minimize, maximize, restore, focus).
        Requires allowlisted app_id and user confirmation.
        """
        if not self.is_automation_enabled():
            res = ActionResult(
                success=False,
                action="window_management",
                message="Access Denied: Desktop automation is disabled in settings.",
            )
            return self._log_result(res)

        clean_act = (action or "").strip().lower()
        if clean_act not in ("minimize", "maximize", "restore", "focus"):
            res = ActionResult(
                success=False,
                action="window_management",
                message=f"Invalid window action '{action}'. Supported: minimize, maximize, restore, focus.",
            )
            return self._log_result(res)

        clean_id = (app_id or "").strip().lower()
        if clean_id not in APPROVED_APPS:
            res = ActionResult(
                success=False,
                action="window_management",
                message=f"Access Denied: Application '{app_id}' is not in approved allowlist.",
            )
            return self._log_result(res)

        app_info = APPROVED_APPS[clean_id]
        if not user_confirmed:
            res = ActionResult(
                success=False,
                action="window_management",
                message=f"Window action '{action}' for {app_info.display_name} requires explicit confirmation.",
                data={"action": clean_act, "app_id": clean_id, "needs_confirmation": True},
            )
            return self._log_result(res)

        if os.name != "nt":
            res = ActionResult(
                success=False,
                action="window_management",
                message="Window management is only supported on Windows.",
                data={"action": clean_act, "app_id": clean_id, "supported": False},
            )
            return self._log_result(res)

        try:
            windows = find_windows_for_executable(app_info.executable)
        except Exception as exc:  # noqa: BLE001 - Win32 boundary
            logger.error("Window enumeration failed for %s: %s", clean_id, exc)
            res = ActionResult(
                success=False,
                action="window_management",
                message=(
                    f"Could not enumerate windows for {app_info.display_name}: "
                    f"{exc}"
                ),
                data={"action": clean_act, "app_id": clean_id},
            )
            return self._log_result(res)

        if not windows:
            res = ActionResult(
                success=False,
                action="window_management",
                message=(
                    f"No visible {app_info.display_name} window was found. "
                    "Launch the application first; no window action was performed."
                ),
                data={
                    "action": clean_act,
                    "app_id": clean_id,
                    "window_found": False,
                    "verified": False,
                },
            )
            return self._log_result(res)

        window = windows[0]
        try:
            verified = apply_verified_window_action(window["handle"], clean_act)
        except Exception as exc:  # noqa: BLE001 - Win32 boundary
            logger.error("Window action failed for %s: %s", clean_id, exc)
            res = ActionResult(
                success=False,
                action="window_management",
                message=(
                    f"Window action '{clean_act}' for {app_info.display_name} "
                    f"failed: {exc}"
                ),
                data={"action": clean_act, "app_id": clean_id, "verified": False},
            )
            return self._log_result(res)

        data = {
            "action": clean_act,
            "app_id": clean_id,
            "window_found": True,
            "window_title": window.get("title", ""),
            "process_id": window.get("process_id"),
            "verified": bool(verified),
        }
        if verified:
            res = ActionResult(
                success=True,
                action="window_management",
                message=(
                    f"Window '{clean_act}' completed and verified for "
                    f"{app_info.display_name}."
                ),
                data=data,
            )
        else:
            res = ActionResult(
                success=False,
                action="window_management",
                message=(
                    f"Window action '{clean_act}' was requested for "
                    f"{app_info.display_name}, but the resulting window state "
                    "could not be verified. It is not reported as successful."
                ),
                data=data,
            )
        return self._log_result(res)

    def get_system_status(self) -> ActionResult:
        """
        Collects system telemetry and resource metrics safely without external dependencies.
        Handles both Windows and non-Windows (Linux/macOS) drive paths gracefully.
        """
        try:
            # Disk usage of system drive
            if platform.system() == "Windows":
                system_drive = os.getenv("SystemDrive", "C:")
                if not system_drive.endswith(("\\", "/")):
                    system_drive += "\\"
            else:
                system_drive = "/"

            total, used, free = shutil.disk_usage(system_drive)
            disk_total_gb = round(total / (1024**3), 1)
            disk_free_gb = round(free / (1024**3), 1)
            disk_used_percent = round((used / total) * 100, 1) if total > 0 else 0.0

            data = {
                "os": platform.system(),
                "os_release": platform.release(),
                "os_version": platform.version(),
                "architecture": platform.machine(),
                "processor": platform.processor(),
                "cpu_count": os.cpu_count() or 1,
                "disk_total_gb": disk_total_gb,
                "disk_free_gb": disk_free_gb,
                "disk_used_percent": disk_used_percent,
                "approved_apps_count": len(APPROVED_APPS),
            }

            summary_msg = (
                f"OS: {data['os']} {data['os_release']} ({data['architecture']}) | "
                f"CPUs: {data['cpu_count']} | "
                f"Drive {system_drive} Free: {disk_free_gb} GB / {disk_total_gb} GB ({disk_used_percent}% used)"
            )

            res = ActionResult(
                success=True,
                action="get_system_status",
                message=summary_msg,
                data=data,
            )
            return self._log_result(res)
        except Exception as e:
            logger.error(f"Error gathering system status: {e}")
            res = ActionResult(
                success=False,
                action="get_system_status",
                message=f"Failed to gather system status: {e}",
            )
            return self._log_result(res)

    def execute_workflow(
        self,
        workflow_id: str,
        user_confirmed: bool = False,
    ) -> ActionResult:
        """
        Executes a registered multi-step workflow.
        """
        if not self.is_automation_enabled():
            res = ActionResult(
                success=False,
                action="execute_workflow",
                message="Access Denied: Desktop automation is disabled in settings.",
            )
            return self._log_result(res)

        clean_id = (workflow_id or "").strip()
        if clean_id not in PRESET_WORKFLOWS:
            res = ActionResult(
                success=False,
                action="execute_workflow",
                message=f"Unknown workflow '{workflow_id}'.",
            )
            return self._log_result(res)

        wf = PRESET_WORKFLOWS[clean_id]
        steps = wf.get("steps", [])
        if not isinstance(steps, (list, tuple)) or not steps:
            res = ActionResult(
                success=False,
                action="execute_workflow",
                message=f"Workflow '{wf['name']}' contains no executable steps.",
                data={"workflow_id": clean_id, "steps": []},
            )
            return self._log_result(res)

        global_confirm = bool(
            get_config_value("automation_require_confirmation", True)
        )
        confirmation_required = bool(
            wf.get("requires_confirmation", False)
        ) or global_confirm
        if confirmation_required and not user_confirmed:
            res = ActionResult(
                success=False,
                action="execute_workflow",
                message=(
                    f"Workflow '{wf['name']}' requires explicit user "
                    "confirmation before any step runs."
                ),
                data={
                    "workflow_id": clean_id,
                    "needs_confirmation": True,
                    "steps": [
                        {
                            "index": index,
                            "action": step.get("action", ""),
                            "parameters": {
                                key: value
                                for key, value in step.items()
                                if key != "action"
                            },
                        }
                        for index, step in enumerate(steps)
                    ],
                },
            )
            return self._log_result(res)

        executed_steps: List[Dict[str, Any]] = []
        failed_step: Optional[Dict[str, Any]] = None
        for index, step in enumerate(steps):
            action_type = step.get("action")
            if action_type == "launch_app":
                step_res = self.launch_app(
                    step.get("app_id", ""), user_confirmed=user_confirmed
                )
            elif action_type == "open_url":
                step_res = self.open_url(
                    step.get("url", ""), user_confirmed=user_confirmed
                )
            else:
                step_res = ActionResult(
                    success=False,
                    action=str(action_type or "unknown"),
                    message=(
                        f"Unsupported workflow step action '{action_type}'; "
                        "the step was not executed."
                    ),
                )

            step_record = step_res.to_dict()
            step_record["index"] = index
            step_record["requested_action"] = action_type
            executed_steps.append(step_record)
            if not step_res.success:
                failed_step = step_record
                break

        navigation_unverified = any(
            step.get("data", {}).get("navigation_verified") is False
            for step in executed_steps
            if isinstance(step.get("data"), dict)
        )
        success = failed_step is None
        data = {
            "workflow_id": clean_id,
            "steps": executed_steps,
            "step_count": len(steps),
            "completed_steps": sum(
                1 for step in executed_steps if step.get("success") is True
            ),
            "failed_step_index": (
                failed_step.get("index") if failed_step is not None else None
            ),
            "overall_success": success,
            "navigation_verified": not navigation_unverified,
        }
        if success:
            message = (
                f"Workflow '{wf['name']}' completed: "
                f"{len(executed_steps)}/{len(steps)} steps succeeded."
            )
            if navigation_unverified:
                message += " Browser navigation was requested but is unverified."
            res = ActionResult(
                success=True,
                action="execute_workflow",
                message=message,
                data=data,
            )
        else:
            failed_index = (
                int(failed_step["index"]) if failed_step is not None else 0
            )
            failed_message = (
                failed_step.get("message", "unknown error")
                if failed_step is not None
                else "unknown error"
            )
            res = ActionResult(
                success=False,
                action="execute_workflow",
                message=(
                    f"Workflow '{wf['name']}' failed at step "
                    f"#{failed_index + 1}: {failed_message}"
                ),
                data=data,
            )
        return self._log_result(res)
