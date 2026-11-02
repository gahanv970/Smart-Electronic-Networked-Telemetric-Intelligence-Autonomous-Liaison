"""
Intelligent Browser Automation Service for S.E.N.T.I.A.L.
Provides safe, controlled browser automation, URL security validation,
readable webpage text extraction, and tab session management.
Guarantees NO execution of arbitrary JavaScript, shell commands, or payment submission.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request
import webbrowser

from config import get_config_value, save_config

logger = logging.getLogger(__name__)


@dataclass
class BrowserTab:
    id: str
    url: str
    title: str
    active: bool = True
    opened_at: str = ""

    def __post_init__(self):
        if not self.opened_at:
            self.opened_at = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BrowserActionResult:
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


class HTMLTextExtractor(HTMLParser):
    """Simple, safe HTML parser that strips scripts, styles, and extracts readable text."""

    def __init__(self):
        super().__init__()
        self._lines = []
        self._ignore = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("script", "style", "noscript", "svg", "header", "footer", "nav"):
            self._ignore = True

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style", "noscript", "svg", "header", "footer", "nav"):
            self._ignore = False

    def handle_data(self, data):
        if not self._ignore:
            text = data.strip()
            if text:
                self._lines.append(text)

    def get_text(self) -> str:
        return " ".join(self._lines)


class BrowserAutomationService:
    """
    Dedicated safe Browser Automation engine.
    Restricts navigation strictly to HTTP/HTTPS, extracts webpage content safely,
    and manages browser tab sessions without executing arbitrary client scripts or form submissions.
    """

    def __init__(self):
        self._tabs: Dict[str, BrowserTab] = {}
        self._activity_log: List[BrowserActionResult] = []

    def is_browser_automation_enabled(self) -> bool:
        """Returns True if browser automation is enabled in application settings."""
        return bool(get_config_value("browser_automation_enabled", True))

    def set_browser_automation_enabled(self, enabled: bool) -> None:
        """Toggles browser automation master enable/disable setting."""
        save_config({"browser_automation_enabled": bool(enabled)})
        logger.info(f"Browser automation master setting updated: enabled={enabled}")

    def get_activity_log(self) -> List[BrowserActionResult]:
        """Returns recent browser action execution logs."""
        return list(self._activity_log)

    def _log_result(self, result: BrowserActionResult) -> BrowserActionResult:
        self._activity_log.append(result)
        if len(self._activity_log) > 100:
            self._activity_log.pop(0)
        return result

    # ------------------------------------------------------------------ URL Validation

    def validate_url(self, url: str) -> Tuple[bool, str, str]:
        """
        Validates URL security.
        Restricts navigation to HTTP/HTTPS (HTTPS preferred).
        Blocks dangerous schemes (file:, javascript:, data:, cmd:, vbscript:).
        Blocks private network/localhost IP redirects (127.0.0.1, 10.x, 192.168.x).
        Returns tuple of (valid: bool, sanitized_url: str, error_message: str).
        """
        clean_url = (url or "").strip()
        if not clean_url:
            return False, "", "URL cannot be empty."

        # Check prohibited scheme candidates
        if ":" in clean_url:
            candidate_scheme = clean_url.split(":", 1)[0].lower()
            if candidate_scheme not in ("http", "https"):
                return False, "", f"Security violation: Scheme '{candidate_scheme}:' is strictly prohibited."

        if not clean_url.startswith(("http://", "https://")):
            clean_url = "https://" + clean_url

        try:
            parsed = urllib.parse.urlparse(clean_url)
            if parsed.scheme.lower() not in ("http", "https"):
                return False, "", f"Security violation: Scheme '{parsed.scheme}:' is prohibited. Only HTTP/HTTPS allowed."

            hostname = (parsed.hostname or "").lower()
            if not hostname:
                return False, "", "Invalid URL: Domain/Host cannot be empty."

            # Block private IP / localhost access
            private_hosts = ["localhost", "127.0.0.1", "0.0.0.0", "::1"]
            if hostname in private_hosts or hostname.endswith(".local") or hostname.endswith(".internal"):
                return False, "", f"Security violation: Access to private/local host '{hostname}' is restricted."

            # Private IP range check
            if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)", hostname):
                return False, "", f"Security violation: Access to private IP address '{hostname}' is restricted."

            return True, clean_url, "URL validated successfully."
        except Exception as e:
            return False, "", f"URL validation error: {e}"

    # ------------------------------------------------------------------ Browser Actions

    def navigate_url(
        self,
        url: str,
        user_confirmed: bool = False,
    ) -> BrowserActionResult:
        """
        Navigates default browser to validated HTTP/HTTPS URL.
        """
        if not self.is_browser_automation_enabled():
            res = BrowserActionResult(
                success=False,
                action="navigate_url",
                message="Access Denied: Browser automation is disabled in settings.",
            )
            return self._log_result(res)

        valid, clean_url, error_msg = self.validate_url(url)
        if not valid:
            res = BrowserActionResult(success=False, action="navigate_url", message=error_msg)
            return self._log_result(res)

        global_confirm = get_config_value("automation_require_confirmation", True)
        if global_confirm and not user_confirmed:
            res = BrowserActionResult(
                success=False,
                action="navigate_url",
                message=f"Navigation to '{clean_url}' requires explicit user confirmation.",
                data={"url": clean_url, "needs_confirmation": True},
            )
            return self._log_result(res)

        try:
            launch_accepted = bool(webbrowser.open(clean_url))
            if not launch_accepted:
                res = BrowserActionResult(
                    success=False,
                    action="navigate_url",
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

            # A tab entry records the validated request, not proof that the
            # browser loaded or rendered the page.
            self.open_tab(clean_url, title="Requested Page (unverified)")
            res = BrowserActionResult(
                success=True,
                action="navigate_url",
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
            logger.error(f"Error navigating URL '{clean_url}': {e}")
            res = BrowserActionResult(
                success=False, action="navigate_url", message=f"Failed to navigate URL: {e}"
            )
            return self._log_result(res)

    def search_web(
        self,
        query: str,
        user_confirmed: bool = False,
    ) -> BrowserActionResult:
        """
        Executes web search on default HTTPS search engine.
        """
        clean_q = (query or "").strip()
        if not clean_q:
            res = BrowserActionResult(success=False, action="search_web", message="Search query cannot be empty.")
            return self._log_result(res)

        encoded_q = urllib.parse.quote_plus(clean_q)
        search_url = f"https://www.google.com/search?q={encoded_q}"
        return self.navigate_url(search_url, user_confirmed=user_confirmed)

    def extract_page_text(
        self,
        url: str,
        timeout: int = 10,
    ) -> BrowserActionResult:
        """
        Safely fetches webpage HTML via HTTPS and extracts readable text.
        CRITICAL SECURITY RULE: Extracted content is treated as UNTRUSTED DATA.
        """
        if not self.is_browser_automation_enabled():
            res = BrowserActionResult(
                success=False,
                action="extract_page_text",
                message="Access Denied: Browser automation is disabled in settings.",
            )
            return self._log_result(res)

        valid, clean_url, error_msg = self.validate_url(url)
        if not valid:
            res = BrowserActionResult(success=False, action="extract_page_text", message=error_msg)
            return self._log_result(res)

        try:
            req = urllib.request.Request(
                clean_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SENTIAL/2.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                html_bytes = response.read(500000)  # Limit max fetch to 500KB
                html_text = html_bytes.decode("utf-8", errors="ignore")

            parser = HTMLTextExtractor()
            parser.feed(html_text)
            extracted_text = parser.get_text()

            # Clean whitespace
            extracted_text = re.sub(r"\s+", " ", extracted_text).strip()
            preview = extracted_text[:1500]

            res = BrowserActionResult(
                success=True,
                action="extract_page_text",
                message=f"Extracted readable text from {clean_url} ({len(extracted_text)} characters).",
                data={"url": clean_url, "extracted_text": preview, "full_length": len(extracted_text)},
            )
            return self._log_result(res)
        except Exception as e:
            logger.warning(f"Failed to extract page text from '{clean_url}': {e}")
            res = BrowserActionResult(
                success=False,
                action="extract_page_text",
                message=f"Failed to extract text from '{clean_url}': {e}",
            )
            return self._log_result(res)

    # ------------------------------------------------------------------ Tab Management

    def open_tab(self, url: str, title: str = "New Tab") -> BrowserTab:
        tab_id = f"tab_{len(self._tabs) + 1}_{int(datetime.now().timestamp() * 1000)}"
        tab = BrowserTab(id=tab_id, url=url, title=title, active=True)
        # Set previous active to False
        for t in self._tabs.values():
            t.active = False
        self._tabs[tab_id] = tab
        return tab

    def get_open_tabs(self) -> List[BrowserTab]:
        return list(self._tabs.values())

    def switch_tab(self, tab_id: str) -> Optional[BrowserTab]:
        if tab_id in self._tabs:
            for t in self._tabs.values():
                t.active = (t.id == tab_id)
            return self._tabs[tab_id]
        return None

    def close_tab(self, tab_id: str) -> bool:
        if tab_id in self._tabs:
            del self._tabs[tab_id]
            return True
        return False
