"""Sequential, permission-aware agent loop for S.E.N.T.I.A.L.

The loop is deliberately small and framework-free:

    user message -> model -> (tool calls, executed one at a time) -> model ...

It owns orchestration only.  Tool registration, validation, permissions,
confirmation grants and structured results all come from
:mod:`engine.tool_registry`; model transport comes from an injected object that
implements ``generate(...)`` (the real Gemini adapter lives in
:mod:`engine.gemini_model`).

Safety properties enforced here
-------------------------------
* Hard limits: 8 tool calls, 60 s per tool call and 5 min per workflow by
  default.  Limits are checked before work is scheduled and are never extended
  silently.
* Tool calls are executed sequentially, never in parallel.
* ``CONFIRMATION_REQUIRED`` tools only run with a grant minted by an
  application-side :class:`~engine.tool_registry.ConfirmationBroker`.  The
  model can ask for the tool but can never authorize it.
* Cancellation stops scheduling immediately.  Python threads cannot be killed,
  so an in-flight handler or model request may finish in the background after
  cancellation; its result is discarded and never reported as success.
* Every terminal path produces a structured :class:`AgentRunResult` with an
  explicit :class:`AgentState`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from engine.tool_registry import (
    ConfirmationBroker,
    ConfirmationError,
    ConfirmationSpoofError,
    Permission,
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolValidationError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AgentConfig",
    "AgentCancelled",
    "AgentConfigurationError",
    "AgentError",
    "AgentEvent",
    "AgentMessage",
    "AgentRunResult",
    "AgentRunner",
    "AgentState",
    "AgentToolContext",
    "CancellationToken",
    "ChatModel",
    "DEFAULT_SYSTEM_INSTRUCTION",
    "ModelResponse",
    "ToolCall",
    "ToolResponse",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentConfigurationError(f"{name} must be a positive number.")
    if value <= 0:
        raise AgentConfigurationError(f"{name} must be a positive number.")
    return float(value)


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentConfigurationError(f"{name} must be a positive integer.")
    if value <= 0:
        raise AgentConfigurationError(f"{name} must be a positive integer.")
    return value


@dataclass(frozen=True)
class AgentConfig:
    """Central execution limits for one workflow.

    ``AgentConfig.from_app_config()`` reads the corresponding keys from
    ``config.DEFAULT_CONFIG`` so the GUI settings dialog and the engine cannot
    drift apart.
    """

    max_tool_calls: int = 8
    tool_timeout_seconds: float = 60.0
    workflow_timeout_seconds: float = 300.0
    max_turns: int = 12

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "max_tool_calls", _positive_int(self.max_tool_calls, "max_tool_calls")
        )
        object.__setattr__(
            self, "max_turns", _positive_int(self.max_turns, "max_turns")
        )
        object.__setattr__(
            self,
            "tool_timeout_seconds",
            _positive_number(self.tool_timeout_seconds, "tool_timeout_seconds"),
        )
        object.__setattr__(
            self,
            "workflow_timeout_seconds",
            _positive_number(
                self.workflow_timeout_seconds, "workflow_timeout_seconds"
            ),
        )

    @classmethod
    def from_mapping(cls, values: Optional[Mapping[str, Any]]) -> "AgentConfig":
        """Build from a config mapping, ignoring unrelated keys."""
        data = dict(values or {})
        return cls(
            max_tool_calls=data.get("agent_max_tool_calls", 8),
            tool_timeout_seconds=data.get("agent_tool_timeout_seconds", 60.0),
            workflow_timeout_seconds=data.get(
                "agent_workflow_timeout_seconds", 300.0
            ),
            max_turns=data.get("agent_max_turns", 12),
        )

    @classmethod
    def from_app_config(cls) -> "AgentConfig":
        """Read the live application configuration without importing Qt."""
        from config import load_config

        return cls.from_mapping(load_config())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_max_tool_calls": self.max_tool_calls,
            "agent_tool_timeout_seconds": self.tool_timeout_seconds,
            "agent_workflow_timeout_seconds": self.workflow_timeout_seconds,
            "agent_max_turns": self.max_turns,
        }


# ---------------------------------------------------------------------------
# States and cancellation
# ---------------------------------------------------------------------------


class AgentState(str, Enum):
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            AgentState.CANCELLED,
            AgentState.COMPLETED,
            AgentState.FAILED,
        }


class AgentError(Exception):
    """Base class for agent-engine errors."""


class AgentConfigurationError(AgentError):
    """Raised for invalid configuration or injected dependencies."""


class AgentCancelled(AgentError):
    """Raised internally when a cancellation token has been set."""


class CancellationToken:
    """Thread-safe cooperative cancellation flag.

    The GUI may call :meth:`cancel` from the Qt main thread while the loop runs
    in a worker thread.  The flag only becomes visible to handlers that receive
    this object; Python cannot forcibly stop a running function.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: Optional[str] = None

    def cancel(self, reason: str = "Cancelled by user") -> bool:
        """Set the flag.  Returns False if it was already cancelled."""
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = str(reason or "Cancelled by user")
            self._event.set()
            return True

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise AgentCancelled(self.reason or "Cancelled by user")

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)


@dataclass(frozen=True)
class AgentToolContext:
    """Opt-in context passed to handlers that declare ``_agent_context``.

    Handlers should poll :attr:`cancellation` during long operations and stop
    cooperatively.  The loop stops waiting immediately on cancellation, but a
    Python thread cannot be killed; cooperative handlers are the only way to
    avoid abandoned background work.
    """

    cancellation: CancellationToken
    deadline_monotonic: float
    turn: int = 0

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())


# ---------------------------------------------------------------------------
# Model contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One function call requested by the model."""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: Optional[str] = None
    thought_signature: Optional[bytes] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise AgentError("Tool call name must be a non-empty string.")
        if not isinstance(self.arguments, Mapping):
            raise AgentError("Tool call arguments must be an object.")
        object.__setattr__(
            self, "name", self.name.strip()
        )


@dataclass(frozen=True)
class ToolResponse:
    """Result payload sent back to the model for one tool call."""

    name: str
    result: Mapping[str, Any]
    call_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.result, Mapping):
            raise AgentError("Tool response result must be an object.")


@dataclass(frozen=True)
class AgentMessage:
    """Provider-neutral conversation turn.

    ``role`` is ``"user"``, ``"model"`` or ``"tool"``.  A model turn may carry
    text, tool calls, or both; a tool turn carries one or more responses.
    """

    role: str
    text: str = ""
    tool_calls: Tuple[ToolCall, ...] = ()
    tool_responses: Tuple[ToolResponse, ...] = ()

    def __post_init__(self) -> None:
        if self.role not in {"user", "model", "tool"}:
            raise AgentError(f"Unsupported message role {self.role!r}.")


@dataclass(frozen=True)
class ModelResponse:
    """What a chat model returns for one generation."""

    text: str = ""
    tool_calls: Tuple[ToolCall, ...] = ()
    finish_reason: str = ""
    raw: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise AgentError("Model response text must be a string.")
        if not isinstance(self.tool_calls, (tuple, list)):
            raise AgentError("Model response tool_calls must be a sequence.")
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))


class ChatModel:
    """Structural interface implemented by :class:`GeminiToolChatModel`."""

    def generate(
        self,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolSpec],
        *,
        system_instruction: str,
        timeout_seconds: Optional[float],
        cancellation: Optional[CancellationToken] = None,
    ) -> ModelResponse:  # pragma: no cover - interface
        raise NotImplementedError


DEFAULT_SYSTEM_INSTRUCTION = (
    "You are S.E.N.T.I.A.L., a Windows desktop assistant. "
    "Use only the tools that are provided. Never invent tool names or arguments. "
    "Tool calls are executed one at a time and their structured result is returned "
    "to you; only report success when that result says success is true. "
    "Confirmation-required actions are approved by the human through the "
    "application UI. You cannot approve them, and you must never add fields such "
    "as user_confirmed, app_confirmed or confirmation_token to tool arguments. "
    "If a tool is denied, cancelled, times out or fails, report that honestly and "
    "do not pretend the action happened."
)


# ---------------------------------------------------------------------------
# Events and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentEvent:
    """Progress notification for the GUI; never contains secrets."""

    state: AgentState
    kind: str
    message: str = ""
    turn: int = 0
    tool_name: Optional[str] = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "data", MappingProxyType(dict(self.data or {}))
        )


@dataclass
class AgentRunResult:
    """Structured outcome of one workflow."""

    state: AgentState
    message: str = ""
    final_text: str = ""
    tool_results: List[ToolResult] = field(default_factory=list)
    turns: int = 0
    tool_call_count: int = 0
    elapsed_seconds: float = 0.0
    error: Optional[str] = None
    error_code: Optional[str] = None
    started_at: str = ""
    finished_at: str = ""

    @property
    def success(self) -> bool:
        """True only when the loop reached a final answer without error."""
        return self.state is AgentState.COMPLETED

    @property
    def cancelled(self) -> bool:
        return self.state is AgentState.CANCELLED

    @property
    def failed_tool_results(self) -> List[ToolResult]:
        return [result for result in self.tool_results if not result.success]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "message": self.message,
            "final_text": self.final_text,
            "tool_results": [result.to_dict() for result in self.tool_results],
            "turns": self.turns,
            "tool_call_count": self.tool_call_count,
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
            "error_code": self.error_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass
class _WaitOutcome:
    value: Any = None
    error: Optional[BaseException] = None
    timed_out: bool = False
    cancelled: bool = False


@dataclass
class _ToolOutcome:
    result: ToolResult
    cancelled: bool = False
    timeout_code: Optional[str] = None
    fatal: bool = False
    error_code: Optional[str] = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _redact(text: Any, limit: int = 500) -> str:
    """Small local redactor so engine errors never echo credentials."""
    import re

    cleaned = str(text)
    patterns = (
        (
            re.compile(
                r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|authorization)"
                r"\s*[:=]\s*\S+"
            ),
            r"\1=[REDACTED]",
        ),
        (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"), "[REDACTED]"),
        (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
    )
    for pattern, replacement in patterns:
        cleaned = pattern.sub(replacement, cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3] + "..."
    return cleaned


def _tool_result_payload(result: ToolResult) -> Dict[str, Any]:
    """The exact, bounded payload the model is allowed to see."""
    return {
        "success": bool(result.success),
        "tool_name": result.tool_name,
        "message": result.message,
        "data": result.data,
        "error": result.error,
        "code": result.code,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class AgentRunner:
    """Runs one user request through a bounded model/tool loop.

    The runner is intentionally synchronous; call :meth:`run` from a worker
    thread (for example ``QThreadPool``) so the Qt event loop stays responsive.
    :meth:`cancel` is safe to call from the GUI thread at any time.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        model: ChatModel,
        *,
        config: Optional[AgentConfig] = None,
        confirmation_broker: Optional[ConfirmationBroker] = None,
        system_instruction: Optional[str] = None,
        listener: Optional[Callable[[AgentEvent], None]] = None,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if not isinstance(registry, ToolRegistry):
            raise AgentConfigurationError("registry must be a ToolRegistry.")
        if not callable(getattr(model, "generate", None)):
            raise AgentConfigurationError("model must implement generate(...).")
        if confirmation_broker is not None and not isinstance(
            confirmation_broker, ConfirmationBroker
        ):
            raise AgentConfigurationError(
                "confirmation_broker must be a ConfirmationBroker or None."
            )
        if listener is not None and not callable(listener):
            raise AgentConfigurationError("listener must be callable or None.")
        poll = _positive_number(poll_interval_seconds, "poll_interval_seconds")
        if poll > 5.0:
            raise AgentConfigurationError(
                "poll_interval_seconds must be at most 5 seconds."
            )

        self._registry = registry
        self._model = model
        self._config = config or AgentConfig()
        if not isinstance(self._config, AgentConfig):
            raise AgentConfigurationError("config must be an AgentConfig.")
        self._broker = confirmation_broker
        self._system_instruction = (
            system_instruction.strip()
            if isinstance(system_instruction, str) and system_instruction.strip()
            else DEFAULT_SYSTEM_INSTRUCTION
        )
        self._listener = listener
        self._poll_interval = poll

        self._state_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._state = AgentState.IDLE
        self._running = False
        self._active_token: Optional[CancellationToken] = None

    # -- Introspection ------------------------------------------------------

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def state(self) -> AgentState:
        with self._state_lock:
            return self._state

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._running

    def set_listener(self, listener: Optional[Callable[[AgentEvent], None]]) -> None:
        if listener is not None and not callable(listener):
            raise AgentConfigurationError("listener must be callable or None.")
        self._listener = listener

    # -- Cancellation -------------------------------------------------------

    def cancel(self, reason: str = "Cancelled by user") -> bool:
        """Request cancellation of the active workflow.

        Returns ``False`` when no workflow is running.  Safe from any thread.
        """
        with self._state_lock:
            token = self._active_token
            if token is None or not self._running:
                return False
            changed = token.cancel(reason)
            if changed and not self._state.is_terminal:
                self._set_state(
                    AgentState.CANCELLING,
                    "Cancellation requested; no new tool calls will be scheduled.",
                )
            return changed

    # -- Public entry point -------------------------------------------------

    def run(
        self,
        user_message: str,
        *,
        cancellation: Optional[CancellationToken] = None,
    ) -> AgentRunResult:
        """Execute one workflow and always return a structured result."""
        if not isinstance(user_message, str) or not user_message.strip():
            raise AgentConfigurationError("user_message must be a non-empty string.")
        if cancellation is not None and not isinstance(cancellation, CancellationToken):
            raise AgentConfigurationError(
                "cancellation must be a CancellationToken or None."
            )

        if not self._run_lock.acquire(blocking=False):
            raise AgentError("AgentRunner is already running a workflow.")
        try:
            with self._state_lock:
                self._running = True
                self._state = AgentState.IDLE
                self._active_token = cancellation or CancellationToken()
                token = self._active_token

            started_monotonic = time.monotonic()
            started_at = _utc_now()
            result: Optional[AgentRunResult] = None

            try:
                result = self._run_workflow(
                    user_message.strip(),
                    token,
                    started_monotonic,
                    started_at,
                )
            except AgentCancelled:
                result = self._finish_cancelled(
                    token, started_monotonic, started_at, "Workflow cancelled."
                )
            except Exception as exc:  # noqa: BLE001 - engine boundary
                logger.exception("Unexpected agent loop failure.")
                result = self._finish_failed(
                    started_monotonic,
                    started_at,
                    "The agent workflow failed unexpectedly.",
                    error_code="internal_error",
                    error=_redact(f"{type(exc).__name__}: {exc}"),
                )
            finally:
                with self._state_lock:
                    self._running = False
                    self._active_token = None

            assert result is not None
            return result
        finally:
            self._run_lock.release()

    # -- Workflow -----------------------------------------------------------

    def _run_workflow(
        self,
        user_message: str,
        token: CancellationToken,
        started: float,
        started_at: str,
    ) -> AgentRunResult:
        if token.is_cancelled:
            self._set_state(AgentState.CANCELLING, token.reason or "Cancelled.")
            return self._finish_cancelled(
                token, started, started_at, token.reason or "Workflow cancelled."
            )

        deadline = started + self._config.workflow_timeout_seconds
        self._set_state(AgentState.PLANNING, "Planning the workflow.")

        history: List[AgentMessage] = [AgentMessage(role="user", text=user_message)]
        tool_results: List[ToolResult] = []
        tool_call_count = 0
        turns = 0

        def fail(
            message: str,
            code: str,
            *,
            error: Optional[str] = None,
            partial: Optional[List[ToolResult]] = None,
        ) -> AgentRunResult:
            return self._finish_failed(
                started,
                started_at,
                message,
                error_code=code,
                error=error,
                partial_results=(
                    list(tool_results) if partial is None else list(partial)
                ),
                turns=turns,
                tool_call_count=tool_call_count,
            )

        def cancelled(message: str) -> AgentRunResult:
            return self._finish_cancelled(
                token,
                started,
                started_at,
                message,
                partial_results=list(tool_results),
                turns=turns,
                tool_call_count=tool_call_count,
            )

        while True:
            turns += 1
            if turns > self._config.max_turns:
                return fail(
                    f"Workflow exceeded the {self._config.max_turns}-turn limit.",
                    "max_turns",
                )

            cancel_reason = self._checkpoint(token, deadline)
            if cancel_reason == "cancelled":
                return cancelled(token.reason or "Workflow cancelled.")
            if cancel_reason == "workflow_timeout":
                return fail(
                    "Workflow exceeded its time limit.", "workflow_timeout"
                )

            self._set_state(AgentState.RUNNING, f"Requesting model turn {turns}.")
            self._emit(
                "model_request",
                "Requesting the next model turn.",
                turn=turns,
            )
            remaining = self._remaining(deadline)
            wait = self._call_in_daemon(
                lambda: self._model.generate(
                    history,
                    self._registry.specs(),
                    system_instruction=self._system_instruction,
                    timeout_seconds=remaining,
                    cancellation=token,
                ),
                remaining,
                token,
            )
            if wait.cancelled:
                return cancelled(token.reason or "Workflow cancelled.")
            if wait.timed_out:
                return fail(
                    "The model did not respond before the workflow deadline.",
                    "workflow_timeout",
                )
            if wait.error is not None:
                if isinstance(wait.error, AgentCancelled):
                    return cancelled(token.reason or "Workflow cancelled.")
                return fail(
                    "The model request failed.",
                    "model_error",
                    error=_redact(f"{type(wait.error).__name__}: {wait.error}"),
                )

            response = wait.value
            if not isinstance(response, ModelResponse):
                return fail(
                    "The model returned an invalid response object.",
                    "invalid_model_response",
                )

            self._emit(
                "model_response",
                "Model response received.",
                turn=turns,
                data={
                    "text_length": len(response.text),
                    "tool_calls": len(response.tool_calls),
                    "finish_reason": response.finish_reason,
                },
            )

            if not response.tool_calls:
                final_text = response.text.strip()
                if not final_text:
                    return fail(
                        "The model returned neither text nor tool calls.",
                        "empty_response",
                    )
                return self._finish_completed(
                    started,
                    started_at,
                    final_text,
                    tool_results,
                    turns,
                    tool_call_count,
                )

            history.append(
                AgentMessage(
                    role="model",
                    text=response.text,
                    tool_calls=response.tool_calls,
                )
            )

            stop_after_failure = False
            for call in response.tool_calls:
                cancel_reason = self._checkpoint(token, deadline)
                if cancel_reason == "cancelled":
                    return cancelled(token.reason or "Workflow cancelled.")
                if cancel_reason == "workflow_timeout":
                    return fail(
                        "Workflow exceeded its time limit.", "workflow_timeout"
                    )

                if tool_call_count >= self._config.max_tool_calls:
                    return fail(
                        "Workflow exceeded the limit of "
                        f"{self._config.max_tool_calls} tool calls.",
                        "tool_call_limit",
                    )

                tool_call_count += 1
                self._emit(
                    "tool_started",
                    f"Executing tool '{call.name}'.",
                    turn=turns,
                    tool_name=call.name,
                    data={"tool_call_number": tool_call_count},
                )

                outcome = self._execute_tool_call(call, token, deadline)
                if outcome.cancelled:
                    tool_results.append(outcome.result)
                    return cancelled(
                        token.reason or "Workflow cancelled during tool execution."
                    )
                if outcome.timeout_code is not None:
                    tool_results.append(outcome.result)
                    return fail(
                        outcome.result.message,
                        outcome.timeout_code,
                        error=outcome.result.error,
                        partial=tool_results,
                    )

                tool_results.append(outcome.result)
                history.append(
                    AgentMessage(
                        role="tool",
                        tool_responses=(
                            ToolResponse(
                                name=call.name,
                                result=_tool_result_payload(outcome.result),
                                call_id=call.call_id,
                            ),
                        ),
                    )
                )
                self._emit(
                    "tool_finished",
                    f"Tool '{call.name}' finished with code '{outcome.result.code}'.",
                    turn=turns,
                    tool_name=call.name,
                    data={
                        "success": outcome.result.success,
                        "code": outcome.result.code,
                        "duration_ms": outcome.result.duration_ms,
                    },
                )

                if outcome.fatal:
                    return fail(
                        outcome.result.message,
                        outcome.error_code or outcome.result.code,
                        error=outcome.result.error,
                        partial=tool_results,
                    )

                if not outcome.result.success:
                    # Do not run the rest of this model turn: a later call may
                    # depend on the one that just failed.  Let the model replan.
                    stop_after_failure = True
                    break

            if stop_after_failure:
                continue

    # -- Tool execution -----------------------------------------------------

    def _execute_tool_call(
        self,
        call: ToolCall,
        token: CancellationToken,
        deadline: float,
    ) -> _ToolOutcome:
        entry = self._registry.find(call.name)
        if entry is None:
            return _ToolOutcome(
                result=ToolResult.failure(
                    call.name,
                    "not_found",
                    f"Unknown tool '{call.name}'; no action was executed.",
                ),
                fatal=False,
                error_code="not_found",
            )

        try:
            validated = self._registry.validate_arguments(call.name, call.arguments)
        except ConfirmationSpoofError as exc:
            return _ToolOutcome(
                result=ToolResult.failure(
                    call.name,
                    "confirmation_spoof",
                    str(exc),
                    error=str(exc),
                ),
                fatal=False,
                error_code="confirmation_spoof",
            )
        except (ToolValidationError, ToolNotFoundError) as exc:
            return _ToolOutcome(
                result=ToolResult.failure(
                    call.name,
                    "validation_error",
                    str(exc),
                    error=str(exc),
                ),
                fatal=False,
                error_code="validation_error",
            )

        grant = None
        if entry.spec.permission is Permission.CONFIRMATION_REQUIRED:
            if self._broker is None:
                return _ToolOutcome(
                    result=ToolResult.failure(
                        call.name,
                        "confirmation_unavailable",
                        f"Tool '{call.name}' requires confirmation, but no "
                        "application confirmation broker is configured. "
                        "The action was not executed.",
                    ),
                    fatal=True,
                    error_code="confirmation_unavailable",
                )

            self._set_state(
                AgentState.WAITING_FOR_CONFIRMATION,
                f"Waiting for application confirmation of '{call.name}'.",
            )
            self._emit(
                "confirmation_requested",
                f"Waiting for application confirmation of '{call.name}'.",
                tool_name=call.name,
            )

            try:
                grant = self._broker.request(call.name, validated)
            except (ToolValidationError, ToolNotFoundError, ConfirmationError) as exc:
                self._set_state(AgentState.RUNNING, "Confirmation request rejected.")
                return _ToolOutcome(
                    result=ToolResult.failure(
                        call.name,
                        "confirmation_invalid",
                        f"Confirmation could not be granted: {exc}",
                        error=_redact(exc),
                    ),
                    fatal=True,
                    error_code="confirmation_invalid",
                )
            except Exception as exc:  # noqa: BLE001 - fail closed
                logger.exception("Confirmation broker failed for tool '%s'.", call.name)
                self._set_state(AgentState.RUNNING, "Confirmation request failed.")
                return _ToolOutcome(
                    result=ToolResult.failure(
                        call.name,
                        "confirmation_error",
                        "The application confirmation flow failed; the action "
                        "was not executed.",
                        error=_redact(f"{type(exc).__name__}: {exc}"),
                    ),
                    fatal=True,
                    error_code="confirmation_error",
                )

            if token.is_cancelled:
                return _ToolOutcome(
                    result=ToolResult.failure(
                        call.name,
                        "cancelled",
                        "Workflow cancelled while waiting for confirmation; the "
                        "action was not executed.",
                    ),
                    cancelled=True,
                )

            if grant is None:
                self._set_state(AgentState.RUNNING, "Confirmation denied.")
                self._emit(
                    "confirmation_denied",
                    f"User denied confirmation for '{call.name}'.",
                    tool_name=call.name,
                )
                return _ToolOutcome(
                    result=ToolResult.failure(
                        call.name,
                        "confirmation_denied",
                        f"User denied confirmation for '{call.name}'. The action "
                        "was not executed.",
                    ),
                    fatal=True,
                    error_code="confirmation_denied",
                )

            self._set_state(AgentState.RUNNING, "Confirmation granted.")
            self._emit(
                "confirmation_granted",
                f"Application approved one execution of '{call.name}'.",
                tool_name=call.name,
            )

        remaining = self._remaining(deadline)
        if remaining <= 0:
            return _ToolOutcome(
                result=ToolResult.failure(
                    call.name,
                    "workflow_timeout",
                    "The workflow deadline expired before the tool could run.",
                ),
                timeout_code="workflow_timeout",
                fatal=True,
            )

        tool_budget = self._config.tool_timeout_seconds
        if entry.spec.timeout_seconds is not None:
            tool_budget = min(tool_budget, float(entry.spec.timeout_seconds))
        effective_budget = min(tool_budget, remaining)
        deadline_limited = remaining <= tool_budget

        wait = self._call_in_daemon(
            lambda: self._registry.execute(
                call.name,
                call.arguments,
                confirmation=grant,
                context=AgentToolContext(
                    cancellation=token,
                    deadline_monotonic=deadline,
                ),
            ),
            effective_budget,
            token,
        )

        if wait.cancelled:
            return _ToolOutcome(
                result=ToolResult.failure(
                    call.name,
                    "cancelled",
                    "Workflow cancelled while the tool was running. The handler "
                    "was abandoned and any external effect must be verified.",
                ),
                cancelled=True,
            )
        if wait.timed_out:
            code = "workflow_timeout" if deadline_limited else "tool_timeout"
            return _ToolOutcome(
                result=ToolResult.failure(
                    call.name,
                    code,
                    (
                        "The workflow deadline expired while the tool was running."
                        if code == "workflow_timeout"
                        else f"Tool '{call.name}' exceeded its "
                        f"{effective_budget:.2f}-second timeout."
                    ),
                    error="The tool handler did not finish; its external effect "
                    "must be verified before retrying.",
                ),
                timeout_code=code,
                fatal=True,
                error_code=code,
            )
        if wait.error is not None:
            return _ToolOutcome(
                result=ToolResult.failure(
                    call.name,
                    "execution_error",
                    f"Tool '{call.name}' failed while executing.",
                    error=_redact(
                        f"{type(wait.error).__name__}: {wait.error}"
                    ),
                ),
                fatal=False,
                error_code="execution_error",
            )

        if not isinstance(wait.value, ToolResult):
            return _ToolOutcome(
                result=ToolResult.failure(
                    call.name,
                    "execution_error",
                    f"Tool '{call.name}' returned an invalid result object.",
                ),
                fatal=True,
                error_code="execution_error",
            )

        return _ToolOutcome(result=wait.value, fatal=False)

    # -- Deadline / cancellation / daemon helper ----------------------------

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _checkpoint(self, token: CancellationToken, deadline: float) -> Optional[str]:
        if token.is_cancelled:
            return "cancelled"
        if self._remaining(deadline) <= 0:
            return "workflow_timeout"
        return None

    def _call_in_daemon(
        self,
        fn: Callable[[], Any],
        timeout_seconds: float,
        token: CancellationToken,
    ) -> _WaitOutcome:
        """Run ``fn`` in a daemon thread with a hard wait budget.

        The thread cannot be killed.  On timeout or cancellation it is
        abandoned deliberately so the GUI stays responsive; callers must not
        report its eventual result as success.
        """
        if token.is_cancelled:
            return _WaitOutcome(cancelled=True)
        if timeout_seconds <= 0:
            return _WaitOutcome(timed_out=True)

        box: Dict[str, Any] = {}

        def target() -> None:
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised by caller
                box["error"] = exc

        worker = threading.Thread(
            target=target,
            name="sential-agent-call",
            daemon=True,
        )
        worker.start()

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            if token.is_cancelled:
                return _WaitOutcome(cancelled=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if worker.is_alive():
                    return _WaitOutcome(timed_out=True)
                break
            worker.join(min(self._poll_interval, remaining))
            if not worker.is_alive():
                break

        if "error" in box:
            return _WaitOutcome(error=box["error"])
        return _WaitOutcome(value=box.get("value"))

    # -- Terminal result builders -------------------------------------------

    def _finish_completed(
        self,
        started: float,
        started_at: str,
        final_text: str,
        tool_results: List[ToolResult],
        turns: int,
        tool_call_count: int,
    ) -> AgentRunResult:
        self._set_state(AgentState.COMPLETED, "Workflow completed.")
        self._emit("completed", "Workflow completed.", turn=turns)
        return AgentRunResult(
            state=AgentState.COMPLETED,
            message="Workflow completed.",
            final_text=final_text,
            tool_results=list(tool_results),
            turns=turns,
            tool_call_count=tool_call_count,
            elapsed_seconds=self._elapsed(started),
            started_at=started_at,
            finished_at=_utc_now(),
        )

    def _finish_failed(
        self,
        started: float,
        started_at: str,
        message: str,
        *,
        error_code: str,
        error: Optional[str] = None,
        partial_results: Optional[List[ToolResult]] = None,
        turns: int = 0,
        tool_call_count: int = 0,
    ) -> AgentRunResult:
        self._set_state(AgentState.FAILED, message)
        self._emit(
            "failed",
            message,
            data={"code": error_code},
        )
        return AgentRunResult(
            state=AgentState.FAILED,
            message=message,
            tool_results=list(partial_results or []),
            turns=turns,
            tool_call_count=tool_call_count,
            elapsed_seconds=self._elapsed(started),
            error=error if error is not None else message,
            error_code=error_code,
            started_at=started_at,
            finished_at=_utc_now(),
        )

    def _finish_cancelled(
        self,
        token: CancellationToken,
        started: float,
        started_at: str,
        message: str,
        *,
        partial_results: Optional[List[ToolResult]] = None,
        turns: int = 0,
        tool_call_count: int = 0,
    ) -> AgentRunResult:
        if self.state is not AgentState.CANCELLING:
            self._set_state(AgentState.CANCELLING, message)
        self._set_state(AgentState.CANCELLED, message)
        self._emit("cancelled", message)
        return AgentRunResult(
            state=AgentState.CANCELLED,
            message=message,
            tool_results=list(partial_results or []),
            turns=turns,
            tool_call_count=tool_call_count,
            elapsed_seconds=self._elapsed(started),
            error=token.reason or message,
            error_code="cancelled",
            started_at=started_at,
            finished_at=_utc_now(),
        )

    @staticmethod
    def _elapsed(started: float) -> float:
        return round(max(0.0, time.monotonic() - started), 3)

    # -- Event/state plumbing ----------------------------------------------

    def _set_state(self, state: AgentState, message: str = "") -> None:
        with self._state_lock:
            previous = self._state
            self._state = state
        if previous is not state:
            self._emit("state_changed", message, data={"previous": previous.value})
        elif message:
            self._emit("state", message)

    def _emit(
        self,
        kind: str,
        message: str = "",
        *,
        turn: int = 0,
        tool_name: Optional[str] = None,
        data: Optional[Mapping[str, Any]] = None,
    ) -> None:
        listener = self._listener
        if listener is None:
            return
        event = AgentEvent(
            state=self.state,
            kind=kind,
            message=message,
            turn=turn,
            tool_name=tool_name,
            data=data or {},
        )
        try:
            listener(event)
        except Exception:  # noqa: BLE001 - UI must not break the workflow
            logger.exception("Agent event listener failed for event '%s'.", kind)
