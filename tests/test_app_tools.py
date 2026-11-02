"""Offline tests for the reviewed application tool registry.

All services are in-process fakes.  No task database, memory database, network,
Gemini call or Windows API is touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from engine.app_tools import APP_TOOL_NAMES, build_app_registry
from engine.tool_registry import (
    ConfirmationBroker,
    Permission,
    ToolRegistry,
    ToolValidationError,
)


class FakeTaskService:
    def __init__(self, tasks: Optional[List[Dict[str, Any]]] = None) -> None:
        self.tasks = {task["id"]: dict(task) for task in (tasks or [])}
        self.created: List[Dict[str, Any]] = []
        self.get_calls: List[int] = []
        self.list_calls: List[Dict[str, Any]] = []
        self.create_error: Optional[BaseException] = None
        self.get_error: Optional[BaseException] = None
        self.list_error: Optional[BaseException] = None
        self.next_id = max(self.tasks, default=0) + 1

    def create_task(self, title, description="", priority="MEDIUM", due_date=None):
        if self.create_error is not None:
            raise self.create_error
        task = {
            "id": self.next_id,
            "title": title,
            "description": description,
            "priority": priority,
            "status": "pending",
            "due_date": due_date,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        self.next_id += 1
        self.tasks[task["id"]] = task
        self.created.append(dict(task))
        return dict(task)

    def get_task(self, task_id):
        self.get_calls.append(task_id)
        if self.get_error is not None:
            raise self.get_error
        task = self.tasks.get(task_id)
        return dict(task) if task is not None else None

    def get_all_tasks(self, status_filter=None, priority_filter=None, search_query=None, sort_by="created_desc"):
        if self.list_error is not None:
            raise self.list_error
        self.list_calls.append(
            {
                "status_filter": status_filter,
                "priority_filter": priority_filter,
                "search_query": search_query,
            }
        )
        tasks = list(self.tasks.values())
        if status_filter:
            tasks = [task for task in tasks if task["status"] == status_filter]
        if priority_filter:
            tasks = [task for task in tasks if task["priority"] == priority_filter]
        if search_query:
            needle = search_query.lower()
            tasks = [
                task
                for task in tasks
                if needle in task["title"].lower()
                or needle in task["description"].lower()
            ]
        return tasks


class FakeMemoryService:
    def __init__(self, memories: Optional[List[Dict[str, Any]]] = None) -> None:
        self.memories = list(memories or [])
        self.enabled = True
        self.calls: List[Dict[str, Any]] = []

    def is_memory_enabled(self):
        return self.enabled

    def get_all_memories(self, category=None, search_query=None):
        self.calls.append({"category": category, "search_query": search_query})
        results = list(self.memories)
        if category:
            results = [item for item in results if item["category"] == category]
        if search_query:
            needle = search_query.lower()
            results = [
                item for item in results if needle in item["content"].lower()
            ]
        return results


class FakeAutomationService:
    def __init__(self) -> None:
        self.result = SimpleNamespace(
            success=True,
            message="System status retrieved.",
            data={"os": "Windows", "cpu_count": 8},
        )
        self.error: Optional[BaseException] = None
        self.calls = 0

    def get_system_status(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class FakeBrowserService:
    def __init__(self) -> None:
        self.result = SimpleNamespace(
            success=True,
            message="Browser launch requested.",
            data={"url": "https://www.youtube.com", "launch_accepted": True},
        )
        self.calls: List[Dict[str, Any]] = []

    def navigate_url(self, url, user_confirmed=False):
        self.calls.append({"url": url, "user_confirmed": user_confirmed})
        return self.result


@pytest.fixture
def services():
    task = FakeTaskService(
        [
            {
                "id": 1,
                "title": "Review Phase 3",
                "description": "Check the agent integration",
                "priority": "HIGH",
                "status": "pending",
                "due_date": None,
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            },
            {
                "id": 2,
                "title": "Write report",
                "description": "Summarize results",
                "priority": "MEDIUM",
                "status": "completed",
                "due_date": None,
                "created_at": "2026-01-02T00:00:00",
                "updated_at": "2026-01-02T00:00:00",
            },
            {
                "id": 3,
                "title": "Buy milk",
                "description": "",
                "priority": "LOW",
                "status": "pending",
                "due_date": None,
                "created_at": "2026-01-03T00:00:00",
                "updated_at": "2026-01-03T00:00:00",
            },
        ]
    )
    memory = FakeMemoryService(
        [
            {
                "id": 10,
                "category": "fact",
                "content": "The user prefers concise reports.",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        ]
    )
    automation = FakeAutomationService()
    browser = FakeBrowserService()
    registry = build_app_registry(
        task_service=task,
        memory_service=memory,
        automation_service=automation,
        browser_service=browser,
    )
    return registry, task, memory, automation, browser


def test_only_reviewed_tools_are_registered(services):
    registry, _, _, _, _ = services
    assert isinstance(registry, ToolRegistry)
    assert set(registry.names()) == set(APP_TOOL_NAMES)

    forbidden = {
        "window_management",
        "execute_workflow",
        "run_shell",
        "run_python",
        "extract_page_text",
        "send_email",
        "send_whatsapp",
        "delete_task",
        "delete_memory",
        "launch_app",
        "open_path",
    }
    assert forbidden.isdisjoint(set(registry.names()))


def test_permissions_match_the_review(services):
    registry, _, _, _, _ = services
    assert registry.get("create_task").spec.permission is Permission.CONFIRMATION_REQUIRED
    assert registry.get("open_url").spec.permission is Permission.CONFIRMATION_REQUIRED
    for name in ("get_task", "list_tasks", "search_memories", "get_system_status"):
        assert registry.get(name).spec.permission is Permission.READ_ONLY


def test_create_task_requires_confirmation_and_does_not_run_without_a_grant(services):
    registry, task, _, _, _ = services

    result = registry.execute("create_task", {"title": "Review Phase 3"})

    assert result.success is False
    assert result.code == "confirmation_required"
    assert task.created == []


def test_create_task_approved_grant_persists_and_reports_real_id(services):
    registry, task, _, _, _ = services
    broker = ConfirmationBroker(registry, lambda request: True)

    grant = broker.request("create_task", {"title": "Review Phase 3"})
    assert grant is not None

    result = registry.execute(
        "create_task",
        {"title": "Review Phase 3"},
        confirmation=grant,
    )

    assert result.success is True
    assert result.code == "ok"
    assert result.data["id"] == task.created[0]["id"]
    assert "Task #" in result.message
    assert len(task.created) == 1


def test_create_task_denied_by_human_does_not_execute(services):
    registry, task, _, _, _ = services
    broker = ConfirmationBroker(registry, lambda request: False)

    grant = broker.request("create_task", {"title": "Review Phase 3"})

    assert grant is None
    assert task.created == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"title": "   "},
        {"title": "Review", "priority": "URGENT"},
        {"title": "Review", "due_date": "2026-13-40"},
        {"title": "Review", "description": "x" * 2001},
    ],
)
def test_create_task_invalid_arguments_are_rejected(services, arguments):
    registry, task, _, _, _ = services
    broker = ConfirmationBroker(registry, lambda request: True)
    try:
        grant = broker.request("create_task", arguments)
    except ToolValidationError:
        # Schema-level rejection happens before the approver is called.
        assert task.created == []
        return

    result = registry.execute("create_task", arguments, confirmation=grant)
    assert result.success is False
    assert result.code == "validation_error"
    assert task.created == []


def test_get_task_success_not_found_and_invalid_id(services):
    registry, task, _, _, _ = services

    found = registry.execute("get_task", {"task_id": 1})
    assert found.success is True
    assert found.data["title"] == "Review Phase 3"

    missing = registry.execute("get_task", {"task_id": 999})
    assert missing.success is False
    assert missing.code == "not_found"

    invalid = registry.execute("get_task", {"task_id": 0})
    assert invalid.success is False
    assert invalid.code == "validation_error"
    assert task.get_calls == [1, 999]


def test_list_tasks_filters_and_limit(services):
    registry, task, _, _, _ = services

    result = registry.execute("list_tasks", {"limit": 2})
    assert result.success is True
    assert result.data["count"] == 3
    assert result.data["returned"] == 2
    assert len(result.data["tasks"]) == 2

    filtered = registry.execute(
        "list_tasks",
        {"status": "pending", "priority": "HIGH", "search": "Phase"},
    )
    assert filtered.success is True
    assert filtered.data["count"] == 1
    assert filtered.data["tasks"][0]["id"] == 1
    assert task.list_calls[-1] == {
        "status_filter": "pending",
        "priority_filter": "HIGH",
        "search_query": "Phase",
    }

    invalid = registry.execute("list_tasks", {"limit": 0})
    assert invalid.success is False
    assert invalid.code == "validation_error"


def test_memory_search_is_denied_without_reading_when_disabled(services):
    registry, _, memory, _, _ = services

    enabled = registry.execute("search_memories", {"query": "concise"})
    assert enabled.success is True
    assert enabled.data["count"] == 1
    assert memory.calls == [{"category": None, "search_query": "concise"}]

    memory.enabled = False
    disabled = registry.execute("search_memories", {"query": "concise"})
    assert disabled.success is False
    assert disabled.code == "memory_disabled"
    # No additional read was made while memory was disabled.
    assert len(memory.calls) == 1


def test_system_status_success_and_failure(services):
    registry, _, _, automation, _ = services

    success = registry.execute("get_system_status", {})
    assert success.success is True
    assert success.data["os"] == "Windows"

    automation.result = SimpleNamespace(
        success=False,
        message="Telemetry unavailable.",
        data=None,
    )
    failed = registry.execute("get_system_status", {})
    assert failed.success is False
    assert failed.code == "service_error"
    assert "Telemetry unavailable" in failed.message

    automation.error = RuntimeError("api_key=super-secret")
    raised = registry.execute("get_system_status", {})
    assert raised.success is False
    assert raised.code == "service_error"
    assert "super-secret" not in (raised.error or "")
    assert "REDACTED" in (raised.error or "")


def test_tool_exceptions_and_unknown_tools_fail_closed(services):
    registry, task, _, _, _ = services

    task.get_error = RuntimeError("database is locked; token=abc123secret")
    failed = registry.execute("get_task", {"task_id": 1})
    assert failed.success is False
    assert failed.code == "service_error"
    assert "abc123secret" not in (failed.error or "")
    assert "REDACTED" in (failed.error or "")

    unknown = registry.execute("delete_task", {"task_id": 1})
    assert unknown.success is False
    assert unknown.code == "not_found"


def test_invalid_service_result_is_not_reported_as_success(services):
    registry, task, _, _, _ = services
    task.create_task = lambda **kwargs: SimpleNamespace(no_mapping=True)
    broker = ConfirmationBroker(registry, lambda request: True)
    grant = broker.request("create_task", {"title": "Review"})

    result = registry.execute(
        "create_task", {"title": "Review"}, confirmation=grant
    )

    assert result.success is False
    assert result.code == "service_error"


def test_open_url_requires_confirmation_and_uses_validated_browser_service(services):
    registry, _, _, _, browser = services

    denied = registry.execute("open_url", {"url": "https://www.youtube.com"})
    assert denied.success is False
    assert denied.code == "confirmation_required"
    assert browser.calls == []

    broker = ConfirmationBroker(registry, lambda request: True)
    grant = broker.request("open_url", {"url": "https://www.youtube.com"})
    result = registry.execute(
        "open_url",
        {"url": "https://www.youtube.com"},
        confirmation=grant,
    )

    assert result.success is True
    assert result.data["url"] == "https://www.youtube.com"
    assert browser.calls == [
        {"url": "https://www.youtube.com", "user_confirmed": True}
    ]


def test_open_url_surfaces_browser_failures(services):
    registry, _, _, _, browser = services
    browser.result = SimpleNamespace(
        success=False,
        message="Access Denied: Browser automation is disabled in settings.",
        data={"launch_accepted": False},
    )
    broker = ConfirmationBroker(registry, lambda request: True)
    grant = broker.request("open_url", {"url": "https://www.youtube.com"})

    result = registry.execute(
        "open_url",
        {"url": "https://www.youtube.com"},
        confirmation=grant,
    )

    assert result.success is False
    assert result.code == "service_error"
    assert "disabled in settings" in result.message
