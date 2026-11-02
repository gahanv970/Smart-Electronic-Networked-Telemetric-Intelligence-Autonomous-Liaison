"""Thread-safe bridge between the agent worker and the Qt confirmation UI.

The :class:`~engine.tool_registry.ConfirmationBroker` calls its approver
synchronously from whatever thread is executing the tool.  That thread is a
worker thread, so it must never create or show a ``QMessageBox`` directly.

This module provides the application-side half of that boundary:

    worker thread                         Qt GUI thread
    -------------                         -------------
    approve(request)
      -> create unique request id
      -> emit confirmation_requested  ->  slot shows a non-modal dialog
      -> wait on a threading.Event         user clicks Yes/No
      <- resolve(id, approved)       <-  resolve() wakes the worker
      -> return True only for exact True

Safety properties
-----------------
* Approval is only ever returned when the GUI calls ``resolve(id, True)`` with
  the exact boolean ``True``.  Timeout, cancellation, shutdown, missing GUI,
  signal-delivery failure and stale IDs all return ``False``.
* Requests are single-use.  ``resolve`` removes the pending request atomically,
  so a late click cannot approve a later operation.
* The worker waits on a ``threading.Event``; it never runs a Qt event loop and
  never blocks the GUI thread.
* Every terminal path removes the pending request, so abandoned dialogs cannot
  leak memory or authorize future calls.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, Signal

from engine.tool_registry import ConfirmationRequest

logger = logging.getLogger(__name__)

__all__ = ["ConfirmationPrompt", "QtConfirmationBridge"]


@dataclass(frozen=True)
class ConfirmationPrompt:
    """Immutable prompt material sent to the GUI thread.

    ``request`` is the broker's already-validated request.  ``request_id`` is
    unique per prompt and is the only value the GUI needs to return.
    """

    request_id: str
    request: ConfirmationRequest
    created_at: float
    expires_at: float

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "request": self.request.to_dict(),
        }


class _PendingConfirmation:
    """Mutable worker-side state for one outstanding prompt."""

    __slots__ = ("prompt", "event", "approved", "reason")

    def __init__(self, prompt: ConfirmationPrompt) -> None:
        self.prompt = prompt
        self.event = threading.Event()
        self.approved = False
        self.reason = "pending"


class QtConfirmationBridge(QObject):
    """Qt-signal bridge used as the ``ConfirmationBroker`` approver.

    Create this object in the Qt GUI thread and pass :meth:`approve` to
    :class:`~engine.tool_registry.ConfirmationBroker`.  Connect
    ``confirmation_requested`` to a GUI slot that shows a dialog and calls
    :meth:`resolve` exactly once with the user's decision.

    The bridge itself never displays UI, which keeps it testable and prevents a
    worker thread from touching widgets.
    """

    #: Emitted from the worker thread; delivered to the GUI thread by Qt.
    confirmation_requested = Signal(object)

    #: Emitted after a prompt is resolved, timed out, cancelled or closed.
    confirmation_closed = Signal(str, str)  # request_id, reason

    #: A bounded cap so a broken caller cannot accumulate unbounded waiters.
    MAX_PENDING = 8

    #: Poll interval while waiting, so cancellation is observed promptly.
    _POLL_SECONDS = 0.05

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(parent)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
        ):
            raise ValueError("timeout_seconds must be a positive number.")
        timeout = float(timeout_seconds)
        if not 0.05 <= timeout <= 600.0:
            raise ValueError(
                "timeout_seconds must be between 0.05 and 600 seconds."
            )

        self._timeout_seconds = timeout
        self._pending: Dict[str, _PendingConfirmation] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._cancellation_checker: Optional[Callable[[], bool]] = None

    # -- Introspection ------------------------------------------------------

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    # -- Worker-side API ----------------------------------------------------

    def approve(self, request: ConfirmationRequest) -> bool:
        """Ask the GUI for approval and block the *worker* until resolved.

        This method is intentionally synchronous because the broker's approver
        contract is synchronous.  It is only safe to call from a worker thread.
        """
        if not isinstance(request, ConfirmationRequest):
            logger.warning("Confirmation bridge received an invalid request type.")
            return False

        now = time.monotonic()
        prompt = ConfirmationPrompt(
            request_id=uuid.uuid4().hex,
            request=request,
            created_at=now,
            expires_at=now + self._timeout_seconds,
        )
        pending = _PendingConfirmation(prompt)

        with self._lock:
            if self._closed:
                return False
            if len(self._pending) >= self.MAX_PENDING:
                logger.warning(
                    "Confirmation bridge rejected a request: too many pending prompts."
                )
                return False
            checker = self._cancellation_checker
            self._pending[prompt.request_id] = pending

        try:
            self.confirmation_requested.emit(prompt)
        except Exception:  # noqa: BLE001 - Qt delivery boundary
            logger.exception("Could not deliver the confirmation request to the GUI.")
            self._discard(prompt.request_id, "bridge_error")
            self._safe_closed_signal(prompt.request_id, "bridge_error")
            return False

        try:
            while True:
                remaining = prompt.expires_at - time.monotonic()
                if remaining <= 0:
                    self._discard(prompt.request_id, "timeout")
                    self._safe_closed_signal(prompt.request_id, "timeout")
                    return False

                if pending.event.wait(min(self._POLL_SECONDS, remaining)):
                    # resolve(), cancel_all() or close() has already set the
                    # decision.  Only exact True counts as approval.
                    return pending.approved is True

                if self._is_cancelled(checker):
                    self._discard(prompt.request_id, "cancelled")
                    self._safe_closed_signal(prompt.request_id, "cancelled")
                    return False

                with self._lock:
                    if self._closed:
                        self._discard(prompt.request_id, "bridge_closed")
                        self._safe_closed_signal(prompt.request_id, "bridge_closed")
                        return False
        finally:
            with self._lock:
                self._pending.pop(prompt.request_id, None)

    # -- GUI-side API -------------------------------------------------------

    def resolve(self, request_id: str, approved: bool) -> bool:
        """Resolve one pending prompt.  Safe to call from the GUI thread.

        Returns ``True`` when this call resolved a live prompt, including a
        denial.  Returns ``False`` for unknown, expired, cancelled or already
        resolved IDs.  A late click therefore cannot authorize a later call.
        """
        if not isinstance(request_id, str) or not request_id:
            return False

        with self._lock:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                return False

            now = time.monotonic()
            if now > pending.prompt.expires_at:
                pending.approved = False
                pending.reason = "expired"
                pending.event.set()
                resolved = False
            elif self._closed:
                pending.approved = False
                pending.reason = "bridge_closed"
                pending.event.set()
                resolved = False
            else:
                pending.approved = approved is True
                pending.reason = "approved" if pending.approved else "denied"
                pending.event.set()
                resolved = True

        self._safe_closed_signal(request_id, pending.reason)
        return resolved

    def cancel_all(self, reason: str = "cancelled") -> int:
        """Deny every pending prompt; used on cancellation and shutdown."""
        clean_reason = str(reason or "cancelled").strip()[:120] or "cancelled"
        with self._lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
            for pending in pending_items:
                pending.approved = False
                pending.reason = clean_reason
                pending.event.set()

        for pending in pending_items:
            self._safe_closed_signal(pending.prompt.request_id, clean_reason)
        return len(pending_items)

    def close(self) -> None:
        """Permanently close the bridge and deny anything still waiting."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.cancel_all("bridge closed")

    def set_cancellation_checker(
        self, checker: Optional[Callable[[], bool]]
    ) -> None:
        """Set a callable polled while waiting, e.g. a ``CancellationToken``.

        A checker that raises is treated as cancelled (fail closed).
        """
        if checker is not None and not callable(checker):
            raise TypeError("cancellation checker must be callable or None.")
        with self._lock:
            self._cancellation_checker = checker

    # -- Internals ----------------------------------------------------------

    def _discard(self, request_id: str, reason: str) -> Optional[_PendingConfirmation]:
        with self._lock:
            pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.approved = False
                pending.reason = reason
                pending.event.set()
        return pending

    @staticmethod
    def _is_cancelled(checker: Optional[Callable[[], bool]]) -> bool:
        if checker is None:
            return False
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001 - cancellation must fail closed
            logger.exception("Confirmation cancellation checker failed.")
            return True

    def _safe_closed_signal(self, request_id: str, reason: str) -> None:
        try:
            self.confirmation_closed.emit(request_id, reason)
        except Exception:  # noqa: BLE001 - observer must not break the worker
            logger.debug("Confirmation closed observer failed.", exc_info=True)
