"""Centralized, permission-aware tool registry for S.E.N.T.I.A.L.

This module is the single trusted entry point for tool registration, JSON-schema
generation, argument validation, permission checking and tool execution.  It is
deliberately framework-free (stdlib only) so the agent loop, the PySide6 GUI and
offline tests can all depend on the same contract.

Trust model
-----------
The LLM is untrusted input.  It may name a tool and supply arguments, but it can
never authorize a consequential action:

* Permission is a property of the *registration*, not of the model request.
* ``CONFIRMATION_REQUIRED`` tools only run when ``execute()`` receives a
  :class:`ConfirmationGrant` that was minted by a :class:`ConfirmationBroker`.
  The broker only mints after an application-side callable (e.g. a Qt dialog)
  returns exactly ``True``.
* Grants are bound to one tool name, one canonical argument fingerprint, an
  expiry and a single use, so they cannot be replayed or widened.
* Argument keys such as ``user_confirmed`` / ``app_confirmed`` are reserved and
  rejected outright, so a model cannot smuggle an approval into its arguments.

This is a structural trust boundary inside one Python process, not an OS-level
sandbox.  Code that can import this module and reach its private sentinels is
already trusted application code.

Limitations by design
---------------------
* No shell, subprocess or filesystem access lives here.  Tools are plain
  callables registered by application code.
* No automatic retries: ``execute()`` runs once and reports the outcome.
* Per-call timeouts belong to the orchestration layer (agent loop), which must
  enforce them without claiming a cancelled call succeeded.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "ConfirmationBroker",
    "ConfirmationError",
    "ConfirmationGrant",
    "ConfirmationRequest",
    "ConfirmationSpoofError",
    "Permission",
    "RegisteredTool",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolSpec",
    "ToolValidationError",
    "get_default_registry",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ToolRegistryError(Exception):
    """Base class for every registry error."""


class ToolRegistrationError(ToolRegistryError):
    """Raised when a tool definition is invalid or duplicates an existing name."""


class ToolNotFoundError(ToolRegistryError):
    """Raised when a tool name is not registered."""


class ToolValidationError(ToolRegistryError):
    """Raised when supplied arguments do not satisfy the tool's JSON schema."""


class ConfirmationSpoofError(ToolValidationError):
    """Raised when arguments contain reserved confirmation fields."""


class ConfirmationError(ToolRegistryError):
    """Raised for invalid, expired, replayed or forged confirmation grants."""


class ToolExecutionError(ToolRegistryError):
    """Raised internally when a handler returns an unusable result."""


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class Permission(str, Enum):
    """How dangerous a tool is, and therefore what it needs before running."""

    READ_ONLY = "READ_ONLY"
    REVERSIBLE = "REVERSIBLE"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"

    @property
    def requires_confirmation(self) -> bool:
        return self is Permission.CONFIRMATION_REQUIRED

    @classmethod
    def from_value(cls, value: Any) -> "Permission":
        """Normalize a permission given as enum member or case-insensitive name."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().upper())
            except ValueError:
                pass
        allowed = ", ".join(member.value for member in cls)
        raise ToolRegistrationError(
            f"Invalid permission {value!r}; expected one of: {allowed}."
        )


# ---------------------------------------------------------------------------
# Immutable helpers
# ---------------------------------------------------------------------------


def _freeze(value: Any) -> Any:
    """Recursively convert dict/list containers into read-only equivalents."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """Deep-copy frozen containers back into plain JSON-compatible values."""
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _fingerprint(arguments: Mapping[str, Any]) -> str:
    """Stable hash of validated arguments, used to bind confirmation grants."""
    return hashlib.sha256(_canonical_json(dict(arguments)).encode("utf-8")).hexdigest()


_REDACTION_PATTERNS = (
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


def _redact(text: Any, limit: int = 400) -> str:
    """Remove common credential shapes and cap length before surfacing errors."""
    cleaned = str(text)
    for pattern, replacement in _REDACTION_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3] + "..."
    return cleaned


# ---------------------------------------------------------------------------
# Tool specification and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """Immutable description of a registered tool."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    permission: Permission = Permission.READ_ONLY
    category: str = "general"
    timeout_seconds: Optional[float] = None

    @property
    def requires_confirmation(self) -> bool:
        return self.permission.requires_confirmation

    def to_json_schema(self) -> Dict[str, Any]:
        """Return a fresh, mutable JSON-schema document for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": _thaw(self.parameters),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": _thaw(self.parameters),
            "permission": self.permission.value,
            "category": self.category,
            "requires_confirmation": self.requires_confirmation,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class RegisteredTool:
    """A tool specification paired with its application-supplied handler."""

    spec: ToolSpec
    handler: Callable[..., Any] = field(repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.spec.name


@dataclass
class ToolResult:
    """Structured outcome of a single tool execution."""

    success: bool
    tool_name: str
    message: str = ""
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    code: str = "ok"
    duration_ms: float = 0.0

    @classmethod
    def failure(
        cls,
        tool_name: str,
        code: str,
        message: str,
        *,
        error: Optional[str] = None,
        duration_ms: float = 0.0,
    ) -> "ToolResult":
        return cls(
            success=False,
            tool_name=tool_name,
            message=message,
            error=error or message,
            code=code,
            duration_ms=duration_ms,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "tool_name": self.tool_name,
            "message": self.message,
            "data": copy.deepcopy(self.data),
            "error": self.error,
            "code": self.code,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# Confirmation: application-side only
# ---------------------------------------------------------------------------

_MINT_KEY = object()

_RESERVED_ARGUMENT_NAMES = frozenset(
    {
        "_agent_context",
        "app_confirmed",
        "approved",
        "approval",
        "confirmed",
        "confirmation",
        "confirmation_grant",
        "confirmation_token",
        "is_confirmed",
        "requires_confirmation",
        "user_approved",
        "user_confirmed",
    }
)


@dataclass(frozen=True)
class ConfirmationRequest:
    """Human-readable prompt material for the application's confirmation UI."""

    tool_name: str
    description: str
    permission: Permission
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        rendered = ", ".join(
            f"{key}={value!r}" for key, value in sorted(dict(self.arguments).items())
        )
        detail = f" ({rendered})" if rendered else ""
        return f"{self.description}{detail}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "description": self.description,
            "permission": self.permission.value,
            "arguments": _thaw(self.arguments),
        }


class ConfirmationGrant:
    """Single-use authorization for one exact tool call.

    Instances can only be minted by :class:`ConfirmationBroker` after the
    application's approver callable returned exactly ``True``.  Constructing a
    grant directly raises :class:`ConfirmationError`.
    """

    __slots__ = (
        "_tool_name",
        "_fingerprint",
        "_issued_at",
        "_expires_at",
        "_used",
        "_scope",
        "_lock",
    )

    def __init__(
        self,
        *,
        tool_name: str,
        fingerprint: str,
        ttl_seconds: float,
        scope: Any = None,
        _key: Any = None,
    ) -> None:
        if _key is not _MINT_KEY:
            raise ConfirmationError(
                "ConfirmationGrant cannot be created directly; use "
                "ConfirmationBroker.request() so a trusted application-side "
                "approver authorizes the action."
            )
        if scope is None:
            raise ConfirmationError("ConfirmationGrant requires a registry scope.")
        if not isinstance(tool_name, str) or not tool_name:
            raise ConfirmationError("ConfirmationGrant requires a tool name.")
        if not isinstance(fingerprint, str) or len(fingerprint) < 16:
            raise ConfirmationError("ConfirmationGrant requires an argument fingerprint.")
        if not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool):
            raise ConfirmationError("ConfirmationGrant requires a numeric TTL.")
        if ttl_seconds <= 0:
            raise ConfirmationError("ConfirmationGrant TTL must be positive.")

        now = time.monotonic()
        self._tool_name = tool_name
        self._fingerprint = fingerprint
        self._issued_at = now
        self._expires_at = now + float(ttl_seconds)
        self._used = False
        self._scope = scope
        self._lock = threading.Lock()

    # -- Read-only metadata -------------------------------------------------

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def issued_at(self) -> float:
        return self._issued_at

    @property
    def expires_at(self) -> float:
        return self._expires_at

    @property
    def used(self) -> bool:
        with self._lock:
            return self._used

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ConfirmationGrant(tool_name={self._tool_name!r}, "
            f"used={self._used}, expires_in={self._expires_at - time.monotonic():.2f}s)"
        )

    # -- Internal consumption ----------------------------------------------

    @classmethod
    def _mint(
        cls,
        tool_name: str,
        fingerprint: str,
        ttl_seconds: float,
        scope: Any,
    ) -> "ConfirmationGrant":
        return cls(
            tool_name=tool_name,
            fingerprint=fingerprint,
            ttl_seconds=ttl_seconds,
            scope=scope,
            _key=_MINT_KEY,
        )

    def _consume(self, tool_name: str, fingerprint: str, scope: Any) -> None:
        """Atomically validate and burn the grant, raising on any mismatch."""
        with self._lock:
            if self._used:
                raise ConfirmationError(
                    "This confirmation grant has already been used."
                )
            if time.monotonic() > self._expires_at:
                self._used = True
                raise ConfirmationError("This confirmation grant has expired.")
            if scope is not self._scope:
                raise ConfirmationError(
                    "This confirmation grant was issued by a different registry."
                )
            if tool_name != self._tool_name:
                raise ConfirmationError(
                    "This confirmation grant was issued for a different tool."
                )
            if not secrets.compare_digest(fingerprint, self._fingerprint):
                raise ConfirmationError(
                    "This confirmation grant was issued for different arguments."
                )
            self._used = True


class ConfirmationBroker:
    """Mints confirmation grants, but only through a trusted approver callback.

    Typical application wiring::

        broker = ConfirmationBroker(registry, approver=qt_confirm_dialog)
        grant = broker.request("send_email", {"to": "..."})
        result = registry.execute("send_email", {"to": "..."}, confirmation=grant)

    ``qt_confirm_dialog`` must be application code (e.g. a ``QMessageBox``) and
    must return exactly ``True`` for approval.  Any other value, exception or
    timeout denies the action.
    """

    def __init__(
        self,
        registry: "ToolRegistry",
        approver: Callable[[ConfirmationRequest], bool],
        *,
        ttl_seconds: float = 120.0,
    ) -> None:
        if not isinstance(registry, ToolRegistry):
            raise TypeError("ConfirmationBroker requires a ToolRegistry instance.")
        if not callable(approver):
            raise TypeError("ConfirmationBroker requires a callable approver.")
        if (
            not isinstance(ttl_seconds, (int, float))
            or isinstance(ttl_seconds, bool)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be a positive number.")
        self._registry = registry
        self._approver = approver
        self._ttl_seconds = float(ttl_seconds)

    def request(
        self,
        tool_name: str,
        arguments: Optional[Mapping[str, Any]] = None,
    ) -> Optional[ConfirmationGrant]:
        """Validate the call, ask the approver, and mint a grant only on approval.

        Raises :class:`ToolNotFoundError`, :class:`ToolValidationError` or
        :class:`ConfirmationError` for programmer errors.  Returns ``None`` when
        the human declined or the approver failed.
        """
        entry = self._registry.get(tool_name)
        if entry.spec.permission is not Permission.CONFIRMATION_REQUIRED:
            raise ConfirmationError(
                f"Tool '{tool_name}' does not require confirmation."
            )

        validated = self._registry.validate_arguments(tool_name, arguments or {})
        request = ConfirmationRequest(
            tool_name=entry.spec.name,
            description=entry.spec.description,
            permission=entry.spec.permission,
            arguments=_freeze(validated),
        )

        try:
            approved = self._approver(request)
        except Exception:
            logger.exception("Confirmation approver failed for tool '%s'.", tool_name)
            return None

        # Strict identity check: truthy strings/dicts are not approval.
        if approved is not True:
            return None

        return ConfirmationGrant._mint(
            entry.spec.name,
            _fingerprint(validated),
            self._ttl_seconds,
            self._registry._confirmation_scope,
        )


# ---------------------------------------------------------------------------
# JSON-schema support (strict subset, no coercion)
# ---------------------------------------------------------------------------

_SUPPORTED_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)

_ALLOWED_SCHEMA_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "const",
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)

_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_matches(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _is_number(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ToolValidationError(f"Unsupported schema type '{expected}'.")


def _type_name(value: Any) -> str:
    return type(value).__name__


def _strict_equal(left: Any, right: Any) -> bool:
    """JSON equality that keeps ``True`` distinct from ``1``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if _is_number(left) and _is_number(right):
        return float(left) == float(right)
    return left == right


def _validate_schema_definition(schema: Any, path: str) -> None:
    """Reject malformed or unsupported schema keywords at registration time."""
    if not isinstance(schema, Mapping):
        raise ToolRegistrationError(f"{path} must be a JSON-schema object.")

    unknown = set(schema) - _ALLOWED_SCHEMA_KEYWORDS
    if unknown:
        raise ToolRegistrationError(
            f"{path} uses unsupported schema keyword(s): {', '.join(sorted(unknown))}."
        )

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, (list, tuple)) else [expected]
        if not types or not all(isinstance(item, str) for item in types):
            raise ToolRegistrationError(f"{path}.type must be a string or list of strings.")
        unsupported = set(types) - _SUPPORTED_TYPES
        if unsupported:
            raise ToolRegistrationError(
                f"{path}.type contains unsupported type(s): "
                f"{', '.join(sorted(unsupported))}."
            )

    for keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if keyword in schema and not _is_number(schema[keyword]):
            raise ToolRegistrationError(f"{path}.{keyword} must be a number.")

    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        if keyword in schema and (
            not isinstance(schema[keyword], int)
            or isinstance(schema[keyword], bool)
            or schema[keyword] < 0
        ):
            raise ToolRegistrationError(f"{path}.{keyword} must be a non-negative integer.")

    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise ToolRegistrationError(f"{path}.pattern must be a string.")
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise ToolRegistrationError(f"{path}.pattern is not a valid regex: {exc}") from exc

    if "enum" in schema:
        value = schema["enum"]
        if not isinstance(value, (list, tuple)) or not value:
            raise ToolRegistrationError(f"{path}.enum must be a non-empty list.")

    if "required" in schema:
        value = schema["required"]
        if not isinstance(value, (list, tuple)):
            raise ToolRegistrationError(f"{path}.required must be a list.")
        if not all(isinstance(item, str) for item in value):
            raise ToolRegistrationError(f"{path}.required must contain only strings.")
        if len(set(value)) != len(value):
            raise ToolRegistrationError(f"{path}.required must not contain duplicates.")

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            raise ToolRegistrationError(f"{path}.properties must be an object.")
        for name, subschema in properties.items():
            if not isinstance(name, str) or not name:
                raise ToolRegistrationError(f"{path}.properties keys must be strings.")
            if name in _RESERVED_ARGUMENT_NAMES:
                raise ToolRegistrationError(
                    f"{path}.properties may not declare reserved "
                    f"field '{name}'."
                )
            _validate_schema_definition(subschema, f"{path}.properties.{name}")

    required = schema.get("required")
    if required is not None and properties is not None:
        missing = [name for name in required if name not in properties]
        if missing:
            raise ToolRegistrationError(
                f"{path}.required references undeclared property(ies): "
                f"{', '.join(missing)}."
            )

    if "items" in schema:
        _validate_schema_definition(schema["items"], f"{path}.items")

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, (bool, Mapping)):
        raise ToolRegistrationError(
            f"{path}.additionalProperties must be a boolean or schema object."
        )
    if isinstance(additional, Mapping):
        _validate_schema_definition(additional, f"{path}.additionalProperties")


def _collect_errors(
    schema: Mapping[str, Any],
    value: Any,
    path: str,
    errors: List[str],
) -> None:
    """Collect every violation instead of failing on the first one."""
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, (list, tuple)) else [expected]
        if not any(_type_matches(item, value) for item in types):
            errors.append(
                f"{path}: expected {' or '.join(types)}, got {_type_name(value)}"
            )
            return

    if "const" in schema and not _strict_equal(value, schema["const"]):
        errors.append(f"{path}: must equal {schema['const']!r}")

    if "enum" in schema and not any(
        _strict_equal(value, candidate) for candidate in schema["enum"]
    ):
        errors.append(
            f"{path}: must be one of "
            f"{', '.join(repr(item) for item in schema['enum'])}"
        )

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(
                f"{path}: must be at least {schema['minLength']} character(s)"
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(
                f"{path}: must be at most {schema['maxLength']} character(s)"
            )
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: must match pattern {schema['pattern']!r}")

    if _is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: must be > {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: must be < {schema['exclusiveMaximum']}")

    if isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: must contain at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: must contain at most {schema['maxItems']} item(s)")
        items_schema = schema.get("items")
        if isinstance(items_schema, Mapping):
            for index, item in enumerate(value):
                _collect_errors(items_schema, item, f"{path}[{index}]", errors)

    if isinstance(value, Mapping):
        properties = schema.get("properties") or {}
        for name in schema.get("required", ()):
            if name not in value:
                errors.append(f"{path}.{name}: required field is missing")
        additional = schema.get("additionalProperties", False)
        for name, item in value.items():
            if not isinstance(name, str):
                errors.append(f"{path}: object keys must be strings")
                continue
            if name in properties:
                _collect_errors(properties[name], item, f"{path}.{name}", errors)
            elif additional is False:
                errors.append(f"{path}.{name}: unexpected field")
            elif isinstance(additional, Mapping):
                _collect_errors(additional, item, f"{path}.{name}", errors)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Instance-scoped registry of validated, permission-classified tools.

    Registries are isolated: registering a name in one instance never affects
    another, which keeps tests and future sub-agents from leaking state.
    """

    MAX_AUDIT_ENTRIES = 200

    def __init__(self, name: str = "default") -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Registry name must be a non-empty string.")
        self._name = name.strip()
        self._tools: Dict[str, RegisteredTool] = {}
        self._lock = threading.RLock()
        # Opaque per-instance token that binds confirmation grants to this
        # registry; a grant minted for registry A cannot authorize registry B.
        self._confirmation_scope = object()
        self._audit: Deque[Dict[str, Any]] = deque(maxlen=self.MAX_AUDIT_ENTRIES)
        self._audit_lock = threading.Lock()

    # -- Introspection ------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def __contains__(self, tool_name: object) -> bool:
        return self.contains(tool_name)

    def __iter__(self):
        return iter(self.names())

    def contains(self, tool_name: str) -> bool:
        if not isinstance(tool_name, str):
            return False
        with self._lock:
            return tool_name in self._tools

    def names(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._tools.keys())

    def get(self, tool_name: str) -> RegisteredTool:
        with self._lock:
            entry = self._tools.get(tool_name)
        if entry is None:
            raise ToolNotFoundError(f"Tool '{tool_name}' is not registered.")
        return entry

    def find(self, tool_name: str) -> Optional[RegisteredTool]:
        with self._lock:
            return self._tools.get(tool_name)

    def specs(self) -> Tuple[ToolSpec, ...]:
        with self._lock:
            return tuple(entry.spec for entry in self._tools.values())

    def json_schemas(self) -> List[Dict[str, Any]]:
        return [spec.to_json_schema() for spec in self.specs()]

    def get_audit_log(self) -> List[Dict[str, Any]]:
        with self._audit_lock:
            return [dict(entry) for entry in self._audit]

    # -- Registration -------------------------------------------------------

    def register(
        self,
        name: str,
        description: str,
        parameters: Optional[Mapping[str, Any]] = None,
        handler: Optional[Callable[..., Any]] = None,
        *,
        permission: Any = Permission.READ_ONLY,
        category: str = "general",
        timeout_seconds: Optional[float] = None,
    ) -> RegisteredTool:
        """Register one tool; reject duplicates and malformed definitions."""
        if not isinstance(name, str) or _TOOL_NAME_PATTERN.match(name) is None:
            raise ToolRegistrationError(
                "Tool name must be 2-64 characters of lowercase letters, digits "
                "or underscores, starting with a letter."
            )
        if not isinstance(description, str) or not description.strip():
            raise ToolRegistrationError("Tool description must be a non-empty string.")
        if not callable(handler):
            raise ToolRegistrationError("Tool handler must be callable.")
        if not isinstance(category, str) or not category.strip():
            raise ToolRegistrationError("Tool category must be a non-empty string.")

        normalized_permission = Permission.from_value(permission)

        if timeout_seconds is not None and (
            not _is_number(timeout_seconds) or timeout_seconds <= 0
        ):
            raise ToolRegistrationError("timeout_seconds must be a positive number.")

        schema = self._normalize_parameters(parameters)

        spec = ToolSpec(
            name=name,
            description=description.strip(),
            parameters=_freeze(schema),
            permission=normalized_permission,
            category=category.strip(),
            timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else None,
        )
        entry = RegisteredTool(spec=spec, handler=handler)

        with self._lock:
            if name in self._tools:
                raise ToolRegistrationError(
                    f"Tool '{name}' is already registered in registry '{self._name}'."
                )
            self._tools[name] = entry

        logger.debug(
            "Registered tool '%s' (permission=%s) in registry '%s'.",
            name,
            normalized_permission.value,
            self._name,
        )
        return entry

    def tool(
        self,
        name: str,
        description: str,
        parameters: Optional[Mapping[str, Any]] = None,
        *,
        permission: Any = Permission.READ_ONLY,
        category: str = "general",
        timeout_seconds: Optional[float] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator form of :meth:`register`; returns the handler unchanged."""

        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.register(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
                permission=permission,
                category=category,
                timeout_seconds=timeout_seconds,
            )
            return handler

        return decorator

    def unregister(self, tool_name: str) -> bool:
        with self._lock:
            return self._tools.pop(tool_name, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._tools.clear()

    @staticmethod
    def _normalize_parameters(
        parameters: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, Mapping):
            raise ToolRegistrationError("parameters must be a JSON-schema object.")

        schema = copy.deepcopy(dict(parameters))
        schema.setdefault("type", "object")
        if schema.get("type") != "object":
            raise ToolRegistrationError(
                "Tool parameters must describe a single JSON object."
            )
        schema.setdefault("properties", {})
        if "additionalProperties" not in schema:
            schema["additionalProperties"] = False
        elif schema["additionalProperties"] is not False:
            raise ToolRegistrationError(
                "additionalProperties must be false: the registry rejects "
                "unexpected arguments."
            )

        _validate_schema_definition(schema, "parameters")
        return schema

    # -- Validation ---------------------------------------------------------

    def validate_arguments(
        self,
        tool_name: str,
        arguments: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Validate and return a detached copy of ``arguments``.

        Raises :class:`ToolNotFoundError`, :class:`ConfirmationSpoofError` or
        :class:`ToolValidationError`.  Values are never coerced: a string "5"
        is not accepted where an integer is required.
        """
        entry = self.get(tool_name)

        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise ToolValidationError("Tool arguments must be a JSON object.")

        keys = set(arguments.keys())
        spoofed = sorted(keys & _RESERVED_ARGUMENT_NAMES)
        if spoofed:
            raise ConfirmationSpoofError(
                "Arguments contain reserved confirmation field(s) "
                f"({', '.join(spoofed)}). Confirmation can only be granted by "
                "the application UI, never by tool arguments."
            )

        errors: List[str] = []
        _collect_errors(entry.spec.parameters, arguments, "arguments", errors)
        if errors:
            raise ToolValidationError("Invalid tool arguments: " + "; ".join(errors))

        return copy.deepcopy(dict(arguments))

    # -- Execution ----------------------------------------------------------

    def execute(
        self,
        tool_name: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        confirmation: Optional[ConfirmationGrant] = None,
        context: Optional[Any] = None,
    ) -> ToolResult:
        """Validate, authorize and run one tool; never raise for tool failures.

        Returns a structured :class:`ToolResult`.  ``context`` is reserved for
        caller data (e.g. a cancellation token) and is not passed to handlers.
        """
        started = time.perf_counter()

        entry = self.find(tool_name)
        if entry is None:
            return ToolResult.failure(
                tool_name,
                "not_found",
                f"Unknown tool '{tool_name}'.",
                duration_ms=self._elapsed_ms(started),
            )

        try:
            validated = self.validate_arguments(tool_name, arguments)
        except ConfirmationSpoofError as exc:
            result = ToolResult.failure(
                tool_name,
                "confirmation_spoof",
                str(exc),
                duration_ms=self._elapsed_ms(started),
            )
            self._record_audit(entry, result, arguments)
            return result
        except ToolValidationError as exc:
            result = ToolResult.failure(
                tool_name,
                "validation_error",
                str(exc),
                duration_ms=self._elapsed_ms(started),
            )
            self._record_audit(entry, result, arguments)
            return result

        if entry.spec.permission.requires_confirmation:
            if not isinstance(confirmation, ConfirmationGrant):
                result = ToolResult.failure(
                    tool_name,
                    "confirmation_required",
                    f"Tool '{tool_name}' requires explicit application-side confirmation.",
                    duration_ms=self._elapsed_ms(started),
                )
                self._record_audit(entry, result, validated)
                return result
            try:
                confirmation._consume(
                    tool_name, _fingerprint(validated), self._confirmation_scope
                )
            except ConfirmationError as exc:
                result = ToolResult.failure(
                    tool_name,
                    "confirmation_invalid",
                    str(exc),
                    duration_ms=self._elapsed_ms(started),
                )
                self._record_audit(entry, result, validated)
                return result
        elif confirmation is not None:
            result = ToolResult.failure(
                tool_name,
                "confirmation_invalid",
                f"Tool '{tool_name}' does not require confirmation; refusing to "
                "consume a grant for it.",
                duration_ms=self._elapsed_ms(started),
            )
            self._record_audit(entry, result, validated)
            return result

        try:
            raw = self._invoke_handler(entry.handler, validated, context)
            result = self._normalize_result(raw, tool_name)
        except Exception as exc:  # noqa: BLE001 - handler boundary
            logger.error(
                "Tool '%s' raised %s.", tool_name, type(exc).__name__
            )
            result = ToolResult.failure(
                tool_name,
                "execution_error",
                f"Tool '{tool_name}' failed while executing.",
                error=_redact(f"{type(exc).__name__}: {exc}"),
                duration_ms=self._elapsed_ms(started),
            )

        result.duration_ms = self._elapsed_ms(started)
        self._record_audit(entry, result, validated)
        return result

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000.0, 3)

    @staticmethod
    def _invoke_handler(
        handler: Callable[..., Any],
        validated: Mapping[str, Any],
        context: Optional[Any],
    ) -> Any:
        """Call a handler, optionally passing an explicit agent context.

        The context is only injected when the handler explicitly declares an
        ``_agent_context`` parameter.  It is never part of the model-visible
        schema and is reserved so a model cannot supply it.  This is the opt-in
        cooperative-cancellation channel for long-running tools.
        """
        kwargs = dict(validated)
        if context is not None:
            accepts = False
            try:
                signature = inspect.signature(handler)
                accepts = "_agent_context" in signature.parameters
            except (TypeError, ValueError):
                accepts = False
            if accepts:
                kwargs["_agent_context"] = context
        return handler(**kwargs)

    @staticmethod
    def _normalize_result(raw: Any, tool_name: str) -> ToolResult:
        if isinstance(raw, ToolResult):
            if not raw.tool_name:
                raw.tool_name = tool_name
            if not raw.success and raw.code == "ok":
                raw.code = "tool_error"
            return raw

        if isinstance(raw, Mapping):
            allowed = {"success", "message", "data", "error", "code"}
            extra = set(raw) - allowed
            if extra:
                raise ToolExecutionError(
                    f"Handler returned unsupported result field(s): "
                    f"{', '.join(sorted(extra))}."
                )
            success = raw.get("success")
            if not isinstance(success, bool):
                raise ToolExecutionError(
                    "Handler result mapping must contain a boolean 'success' field."
                )
            data = raw.get("data")
            if data is not None and not isinstance(data, Mapping):
                raise ToolExecutionError("Handler 'data' must be an object or null.")
            message = raw.get("message", "")
            error = raw.get("error")
            code = raw.get("code", "ok" if success else "tool_error")
            if not isinstance(message, str) or not isinstance(code, str):
                raise ToolExecutionError("Handler 'message' and 'code' must be strings.")
            if error is not None and not isinstance(error, str):
                raise ToolExecutionError("Handler 'error' must be a string or null.")
            return ToolResult(
                success=success,
                tool_name=tool_name,
                message=message,
                data=copy.deepcopy(dict(data)) if data is not None else None,
                error=error,
                code=code,
            )

        if raw is None:
            return ToolResult(success=True, tool_name=tool_name, message="")

        return ToolResult(success=True, tool_name=tool_name, message=str(raw))

    def _record_audit(
        self,
        entry: RegisteredTool,
        result: ToolResult,
        arguments: Any,
    ) -> None:
        """Record metadata only: never raw arguments or handler messages."""
        try:
            fingerprint = _fingerprint(arguments if isinstance(arguments, Mapping) else {})
        except Exception:  # pragma: no cover - defensive
            fingerprint = "unavailable"

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": entry.spec.name,
            "permission": entry.spec.permission.value,
            "success": bool(result.success),
            "code": result.code,
            "duration_ms": result.duration_ms,
            "arguments_fingerprint": fingerprint[:16],
        }
        with self._audit_lock:
            self._audit.append(record)


# ---------------------------------------------------------------------------
# Process-wide default registry (lazily created; no import side effects)
# ---------------------------------------------------------------------------

_DEFAULT_REGISTRY: Optional[ToolRegistry] = None
_DEFAULT_REGISTRY_LOCK = threading.Lock()


def get_default_registry() -> ToolRegistry:
    """Return the process-wide registry, creating it on first use."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        with _DEFAULT_REGISTRY_LOCK:
            if _DEFAULT_REGISTRY is None:
                _DEFAULT_REGISTRY = ToolRegistry("default")
    return _DEFAULT_REGISTRY
