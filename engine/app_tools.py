"""Application tools exposed to the S.E.N.T.I.A.L. agent.

Only a deliberately small set of capabilities is registered here, and each one
is backed by an existing, verified application service:

* :class:`~task_service.TaskService` — create one task (confirmation required),
  look one up by id, or list/search tasks.
* :class:`~memory_service.MemoryService` — search saved memories.  This is only
  available while the user's memory setting is enabled.
* :class:`~automation_service.WindowsAutomationService` — read system telemetry.
* :class:`~browser_service.BrowserAutomationService` — open a validated URL
  after explicit user confirmation.

Deliberately **not** registered here:

* arbitrary shell commands or Python execution;
* unrestricted filesystem access;
* unrestricted browser automation (including ``extract_page_text``);
* ``window_management`` (the existing implementation validates and reports an
  action but does not actually manipulate a window);
* ``execute_workflow`` (it can report an overall result even when individual
  steps fail);
* Gmail or WhatsApp sending/deletion;
* destructive task or memory operations.

Every handler returns a structured mapping that the registry normalizes.  A
success result is only returned after the underlying service returned a
verified, successful object; exceptions and missing objects become failures.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from engine.tool_registry import Permission, ToolRegistrationError, ToolRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "APP_TOOL_NAMES",
    "build_app_registry",
    "register_app_tools",
]

#: The exact allowlist of tools this module registers.
APP_TOOL_NAMES = (
    "create_task",
    "get_task",
    "list_tasks",
    "search_memories",
    "get_system_status",
    "open_url",
)

_VALID_PRIORITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_MAX_TITLE_LENGTH = 200
_MAX_DESCRIPTION_LENGTH = 2000
_MAX_SEARCH_LENGTH = 200
_MAX_MEMORY_CATEGORY_LENGTH = 50
_MAX_RESULT_ITEMS = 50
_MAX_ERROR_LENGTH = 300

_SECRET_PATTERNS = (
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


# ---------------------------------------------------------------------------
# Small result helpers
# ---------------------------------------------------------------------------


def _success(message: str, data: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "success": True,
        "message": str(message or ""),
        "code": "ok",
    }
    if data is not None:
        payload["data"] = dict(data)
    return payload


def _failure(
    code: str,
    message: str,
    *,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "success": False,
        "code": str(code or "tool_error"),
        "message": str(message or "The tool failed."),
        "error": str(error if error is not None else message),
    }


def _safe_error(exc: BaseException) -> str:
    """Redact common credential shapes before surfacing an error string."""
    text = f"{type(exc).__name__}: {exc}"
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > _MAX_ERROR_LENGTH:
        text = text[: _MAX_ERROR_LENGTH - 3] + "..."
    return text


def _service_result_to_dict(value: Any, *, tool_name: str) -> Dict[str, Any]:
    """Convert a task/memory object to a plain dictionary or raise."""
    if isinstance(value, Mapping):
        return dict(value)
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return dict(converted)
    raise TypeError(f"{tool_name} returned an object without a mapping representation.")


def _require_service(service: Any, methods: tuple, label: str) -> None:
    if service is None:
        raise ToolRegistrationError(f"{label} is required to register application tools.")
    missing = [
        name for name in methods if not callable(getattr(service, name, None))
    ]
    if missing:
        raise ToolRegistrationError(
            f"{label} is missing required method(s): {', '.join(missing)}."
        )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_CREATE_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_TITLE_LENGTH,
            "description": "Short task title.",
        },
        "description": {
            "type": "string",
            "maxLength": _MAX_DESCRIPTION_LENGTH,
            "description": "Optional task details.",
        },
        "priority": {
            "type": "string",
            "enum": list(_VALID_PRIORITIES),
            "description": "Task priority. Defaults to MEDIUM.",
        },
        "due_date": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "maxLength": 10,
            "description": "Optional due date in YYYY-MM-DD format.",
        },
    },
    "required": ["title"],
}

_GET_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "integer",
            "minimum": 1,
            "maximum": 2147483647,
            "description": "Numeric id of the task to look up.",
        }
    },
    "required": ["task_id"],
}

_LIST_TASKS_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["pending", "completed"],
            "description": "Optional status filter.",
        },
        "priority": {
            "type": "string",
            "enum": list(_VALID_PRIORITIES),
            "description": "Optional priority filter.",
        },
        "search": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_SEARCH_LENGTH,
            "description": "Optional text to search in task titles and descriptions.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_RESULT_ITEMS,
            "description": "Maximum number of tasks to return. Defaults to 20.",
        },
    },
}

_SEARCH_MEMORIES_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_SEARCH_LENGTH,
            "description": "Text to search for in saved memories.",
        },
        "category": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_MEMORY_CATEGORY_LENGTH,
            "description": "Optional memory category filter.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_RESULT_ITEMS,
            "description": "Maximum number of memories to return. Defaults to 10.",
        },
    },
    "required": ["query"],
}

_SYSTEM_STATUS_SCHEMA = {
    "type": "object",
    "properties": {},
}

_OPEN_URL_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2048,
            "description": (
                "HTTP or HTTPS URL to open. Resolve common site names such as "
                "YouTube to their canonical website URL."
            ),
        }
    },
    "required": ["url"],
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _make_create_task_handler(task_service: Any):
    def create_task(
        title: str,
        description: str = "",
        priority: str = "MEDIUM",
        due_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            clean_title = str(title or "").strip()
            if not clean_title:
                return _failure(
                    "validation_error", "Task title cannot be empty."
                )
            if len(clean_title) > _MAX_TITLE_LENGTH:
                return _failure(
                    "validation_error",
                    f"Task title must be at most {_MAX_TITLE_LENGTH} characters.",
                )

            clean_priority = str(priority or "MEDIUM").strip().upper()
            if clean_priority not in _VALID_PRIORITIES:
                return _failure(
                    "validation_error",
                    "Priority must be one of: " + ", ".join(_VALID_PRIORITIES) + ".",
                )

            clean_description = str(description or "").strip()
            if len(clean_description) > _MAX_DESCRIPTION_LENGTH:
                return _failure(
                    "validation_error",
                    "Task description is too long.",
                )

            clean_due_date = None
            if due_date is not None and str(due_date).strip():
                clean_due_date = str(due_date).strip()
                try:
                    datetime.strptime(clean_due_date, "%Y-%m-%d")
                except ValueError:
                    return _failure(
                        "validation_error",
                        "due_date must be a real date in YYYY-MM-DD format.",
                    )

            task = task_service.create_task(
                title=clean_title,
                description=clean_description,
                priority=clean_priority,
                due_date=clean_due_date,
            )
            if task is None:
                return _failure(
                    "service_error",
                    "The task service did not return a created task.",
                )

            data = _service_result_to_dict(task, tool_name="create_task")
            task_id = data.get("id")
            if (
                isinstance(task_id, bool)
                or not isinstance(task_id, int)
                or task_id <= 0
            ):
                return _failure(
                    "service_error",
                    "The task service did not return a persisted task id; "
                    "the task cannot be confirmed as created.",
                )

            return _success(
                f"Task #{task_id} created: {data.get('title', clean_title)}",
                data,
            )
        except ValueError as exc:
            return _failure("validation_error", _safe_error(exc), error=_safe_error(exc))
        except Exception as exc:  # noqa: BLE001 - tool boundary
            logger.error("Tool 'create_task' failed with %s.", type(exc).__name__)
            return _failure(
                "service_error",
                "The task service failed to create the task.",
                error=_safe_error(exc),
            )

    return create_task


def _make_get_task_handler(task_service: Any):
    def get_task(task_id: int) -> Dict[str, Any]:
        try:
            if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
                return _failure(
                    "validation_error", "task_id must be a positive integer."
                )
            task = task_service.get_task(task_id)
            if task is None:
                return _failure(
                    "not_found", f"No task with id {task_id} was found."
                )
            data = _service_result_to_dict(task, tool_name="get_task")
            return _success(
                f"Task #{task_id}: {data.get('title', '')} "
                f"[{data.get('status', 'unknown')}]",
                data,
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary
            logger.error("Tool 'get_task' failed with %s.", type(exc).__name__)
            return _failure(
                "service_error",
                "The task service failed to look up the task.",
                error=_safe_error(exc),
            )

    return get_task


def _make_list_tasks_handler(task_service: Any):
    def list_tasks(
        status: Optional[str] = None,
        priority: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        try:
            if isinstance(limit, bool) or not isinstance(limit, int):
                return _failure("validation_error", "limit must be an integer.")
            if not 1 <= limit <= _MAX_RESULT_ITEMS:
                return _failure(
                    "validation_error",
                    f"limit must be between 1 and {_MAX_RESULT_ITEMS}.",
                )

            clean_status = str(status).strip().lower() if status else None
            if clean_status not in (None, "pending", "completed"):
                return _failure(
                    "validation_error", "status must be 'pending' or 'completed'."
                )

            clean_priority = str(priority).strip().upper() if priority else None
            if clean_priority not in (None, *_VALID_PRIORITIES):
                return _failure(
                    "validation_error",
                    "priority must be one of: " + ", ".join(_VALID_PRIORITIES) + ".",
                )

            clean_search = str(search).strip() if search else None
            if clean_search is not None and not clean_search:
                clean_search = None
            if clean_search is not None and len(clean_search) > _MAX_SEARCH_LENGTH:
                return _failure("validation_error", "search text is too long.")

            tasks = task_service.get_all_tasks(
                status_filter=clean_status,
                priority_filter=clean_priority,
                search_query=clean_search,
            )
            if not isinstance(tasks, (list, tuple)):
                return _failure(
                    "service_error",
                    "The task service returned an invalid task collection.",
                )

            selected = list(tasks)[:limit]
            task_dicts = [
                _service_result_to_dict(task, tool_name="list_tasks")
                for task in selected
            ]
            return _success(
                f"Found {len(tasks)} task(s); returning {len(task_dicts)}.",
                {
                    "tasks": task_dicts,
                    "count": len(tasks),
                    "returned": len(task_dicts),
                },
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary
            logger.error("Tool 'list_tasks' failed with %s.", type(exc).__name__)
            return _failure(
                "service_error",
                "The task service failed to list tasks.",
                error=_safe_error(exc),
            )

    return list_tasks


def _make_search_memories_handler(memory_service: Any):
    def search_memories(
        query: str,
        category: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        try:
            enabled_check = getattr(memory_service, "is_memory_enabled", None)
            if callable(enabled_check) and not enabled_check():
                return _failure(
                    "memory_disabled",
                    "Long-term memory is disabled in Settings; no memories were read.",
                )

            clean_query = str(query or "").strip()
            if not clean_query:
                return _failure(
                    "validation_error", "Memory search query cannot be empty."
                )
            if len(clean_query) > _MAX_SEARCH_LENGTH:
                return _failure("validation_error", "Memory search query is too long.")

            clean_category = str(category).strip().lower() if category else None
            if clean_category is not None and not clean_category:
                clean_category = None
            if (
                clean_category is not None
                and len(clean_category) > _MAX_MEMORY_CATEGORY_LENGTH
            ):
                return _failure("validation_error", "Memory category is too long.")

            if isinstance(limit, bool) or not isinstance(limit, int):
                return _failure("validation_error", "limit must be an integer.")
            if not 1 <= limit <= _MAX_RESULT_ITEMS:
                return _failure(
                    "validation_error",
                    f"limit must be between 1 and {_MAX_RESULT_ITEMS}.",
                )

            memories = memory_service.get_all_memories(
                category=clean_category,
                search_query=clean_query,
            )
            if not isinstance(memories, (list, tuple)):
                return _failure(
                    "service_error",
                    "The memory service returned an invalid memory collection.",
                )

            selected = list(memories)[:limit]
            memory_dicts = [
                _service_result_to_dict(memory, tool_name="search_memories")
                for memory in selected
            ]
            return _success(
                f"Found {len(memories)} memory item(s); returning {len(memory_dicts)}.",
                {
                    "memories": memory_dicts,
                    "count": len(memories),
                    "returned": len(memory_dicts),
                },
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary
            logger.error(
                "Tool 'search_memories' failed with %s.", type(exc).__name__
            )
            return _failure(
                "service_error",
                "The memory service failed to search memories.",
                error=_safe_error(exc),
            )

    return search_memories


def _make_system_status_handler(automation_service: Any):
    def get_system_status() -> Dict[str, Any]:
        try:
            result = automation_service.get_system_status()
            if result is None:
                return _failure(
                    "service_error",
                    "The system status service returned no result.",
                )

            success = getattr(result, "success", None)
            message = getattr(result, "message", "")
            data = getattr(result, "data", None)

            if success is not True:
                return _failure(
                    "service_error",
                    str(message or "System status could not be read."),
                    error=_safe_error(RuntimeError(str(message or "status failure"))),
                )
            if not isinstance(data, Mapping):
                return _failure(
                    "service_error",
                    "The system status service returned an invalid result object.",
                )

            return _success(str(message or "System status retrieved."), dict(data))
        except Exception as exc:  # noqa: BLE001 - tool boundary
            logger.error(
                "Tool 'get_system_status' failed with %s.", type(exc).__name__
            )
            return _failure(
                "service_error",
                "The system status service failed.",
                error=_safe_error(exc),
            )

    return get_system_status


def _make_open_url_handler(browser_service: Any):
    def open_url(url: str) -> Dict[str, Any]:
        try:
            clean_url = str(url or "").strip()
            if not clean_url:
                return _failure("validation_error", "URL cannot be empty.")
            if len(clean_url) > 2048:
                return _failure("validation_error", "URL is too long.")

            result = browser_service.navigate_url(
                clean_url,
                user_confirmed=True,
            )
            if result is None:
                return _failure(
                    "service_error",
                    "The browser service returned no result.",
                )

            success = getattr(result, "success", None)
            message = str(getattr(result, "message", "") or "")
            data = getattr(result, "data", None)
            if (
                success is not True
                or not isinstance(data, Mapping)
                or data.get("launch_accepted") is not True
            ):
                return _failure(
                    "service_error",
                    message or "The browser could not open the requested URL.",
                    error=_safe_error(RuntimeError(message or "browser launch failed")),
                )

            return _success(message or "The URL was opened in the browser.", dict(data))
        except Exception as exc:  # noqa: BLE001 - tool boundary
            logger.error("Tool 'open_url' failed with %s.", type(exc).__name__)
            return _failure(
                "service_error",
                "The browser service failed to open the URL.",
                error=_safe_error(exc),
            )

    return open_url


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_app_tools(
    registry: ToolRegistry,
    *,
    task_service: Any,
    memory_service: Any,
    automation_service: Any,
    browser_service: Any,
) -> ToolRegistry:
    """Register the reviewed application tools on ``registry``."""
    if not isinstance(registry, ToolRegistry):
        raise ToolRegistrationError("register_app_tools requires a ToolRegistry.")

    _require_service(task_service, ("create_task", "get_task", "get_all_tasks"), "TaskService")
    _require_service(
        memory_service,
        ("get_all_memories",),
        "MemoryService",
    )
    _require_service(
        automation_service,
        ("get_system_status",),
        "AutomationService",
    )
    _require_service(
        browser_service,
        ("navigate_url",),
        "BrowserAutomationService",
    )

    registry.register(
        name="create_task",
        description=(
            "Create one new task in the user's persistent task list. This changes "
            "stored data and always requires explicit user confirmation."
        ),
        parameters=_CREATE_TASK_SCHEMA,
        handler=_make_create_task_handler(task_service),
        permission=Permission.CONFIRMATION_REQUIRED,
        category="tasks",
        timeout_seconds=30.0,
    )
    registry.register(
        name="get_task",
        description=(
            "Look up one task in the user's task list by its numeric id. Read-only."
        ),
        parameters=_GET_TASK_SCHEMA,
        handler=_make_get_task_handler(task_service),
        permission=Permission.READ_ONLY,
        category="tasks",
        timeout_seconds=15.0,
    )
    registry.register(
        name="list_tasks",
        description=(
            "List or search the user's tasks, optionally filtered by status, "
            "priority or text. Read-only."
        ),
        parameters=_LIST_TASKS_SCHEMA,
        handler=_make_list_tasks_handler(task_service),
        permission=Permission.READ_ONLY,
        category="tasks",
        timeout_seconds=15.0,
    )
    registry.register(
        name="search_memories",
        description=(
            "Search the user's saved long-term memories by text and optional "
            "category. Read-only. Unavailable when memory is disabled."
        ),
        parameters=_SEARCH_MEMORIES_SCHEMA,
        handler=_make_search_memories_handler(memory_service),
        permission=Permission.READ_ONLY,
        category="memory",
        timeout_seconds=15.0,
    )
    registry.register(
        name="get_system_status",
        description=(
            "Read basic system status: operating system, CPU count and disk "
            "usage. Read-only."
        ),
        parameters=_SYSTEM_STATUS_SCHEMA,
        handler=_make_system_status_handler(automation_service),
        permission=Permission.READ_ONLY,
        category="system",
        timeout_seconds=15.0,
    )
    registry.register(
        name="open_url",
        description=(
            "Open a validated HTTP/HTTPS website in the user's default browser. "
            "Use the canonical URL for common site names such as YouTube. "
            "Always requires explicit user confirmation."
        ),
        parameters=_OPEN_URL_SCHEMA,
        handler=_make_open_url_handler(browser_service),
        permission=Permission.CONFIRMATION_REQUIRED,
        category="browser",
        timeout_seconds=30.0,
    )

    return registry


def build_app_registry(
    *,
    task_service: Any,
    memory_service: Any,
    automation_service: Any,
    browser_service: Any,
) -> ToolRegistry:
    """Build a fresh registry containing only the reviewed application tools."""
    registry = ToolRegistry("sential-app")
    return register_app_tools(
        registry,
        task_service=task_service,
        memory_service=memory_service,
        automation_service=automation_service,
        browser_service=browser_service,
    )
