"""
Unit tests for Task Management Service:
CRUD operations, priority updates, due dates, filtering, search, and restart persistence.
"""

from pathlib import Path
import tempfile
import pytest

from task_service import TaskService


@pytest.fixture
def temp_task_service():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "tasks_test.db"
        svc = TaskService(db_path=db_path)
        yield svc, db_path


def test_task_create_and_validation(temp_task_service):
    svc, _ = temp_task_service

    # Valid creation
    task = svc.create_task("Finish desktop app", "Review all requirements", priority="HIGH", due_date="2026-11-01")
    assert task.id is not None
    assert task.title == "Finish desktop app"
    assert task.priority == "HIGH"
    assert task.status == "pending"
    assert task.due_date == "2026-11-01"

    # Title validation
    with pytest.raises(ValueError):
        svc.create_task("")

    # Priority validation
    with pytest.raises(ValueError):
        svc.create_task("Valid Title", priority="INVALID_PRIORITY")


def test_task_get_and_filtering(temp_task_service):
    svc, _ = temp_task_service

    t1 = svc.create_task("Task 1", priority="LOW")
    t2 = svc.create_task("Task 2", priority="HIGH")
    t3 = svc.create_task("Task 3", priority="CRITICAL")

    all_tasks = svc.get_all_tasks()
    assert len(all_tasks) == 3

    high_tasks = svc.get_all_tasks(priority_filter="HIGH")
    assert len(high_tasks) == 1
    assert high_tasks[0].id == t2.id

    svc.toggle_task_status(t1.id)
    assert svc.get_task(t1.id).status == "completed"

    pending_tasks = svc.get_all_tasks(status_filter="pending")
    assert len(pending_tasks) == 2


def test_task_search(temp_task_service):
    svc, _ = temp_task_service
    svc.create_task("Write unit tests", "Pytest suite for python desktop")
    svc.create_task("Buy groceries", "Milk and coffee")

    results = svc.get_all_tasks(search_query="pytest")
    assert len(results) == 1
    assert results[0].title == "Write unit tests"


def test_task_update_and_delete(temp_task_service):
    svc, _ = temp_task_service
    task = svc.create_task("Initial Title", priority="LOW")

    updated = svc.update_task(task.id, title="Updated Title", priority="CRITICAL", due_date="2026-12-31")
    assert updated.title == "Updated Title"
    assert updated.priority == "CRITICAL"
    assert updated.due_date == "2026-12-31"

    deleted = svc.delete_task(task.id)
    assert deleted is True
    assert svc.get_task(task.id) is None


def test_task_restart_persistence(temp_task_service):
    svc1, db_path = temp_task_service
    t1 = svc1.create_task("Persistent Task 1", priority="HIGH")
    t2 = svc1.create_task("Persistent Task 2", priority="MEDIUM")

    # Simulate application restart with a fresh service instance on same database
    svc2 = TaskService(db_path=db_path)
    reloaded = svc2.get_all_tasks()
    assert len(reloaded) == 2
    assert reloaded[0].title in ["Persistent Task 1", "Persistent Task 2"]
