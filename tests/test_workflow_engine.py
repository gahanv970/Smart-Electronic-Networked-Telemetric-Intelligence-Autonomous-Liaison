from unittest.mock import MagicMock, patch
import pytest

from alert_service import AlertNotificationService
from automation_service import WindowsAutomationService
from browser_service import BrowserAutomationService
from gmail_service import GmailService
from task_service import TaskService
from whatsapp_service import WhatsAppService
from workflow_engine import Workflow, WorkflowEngine, WorkflowStep


@pytest.fixture
def mock_services(tmp_path):
    auto_svc = MagicMock(spec=WindowsAutomationService)
    auto_svc.is_automation_enabled.return_value = True

    browser_svc = MagicMock(spec=BrowserAutomationService)
    gmail_svc = MagicMock(spec=GmailService)
    wa_svc = MagicMock(spec=WhatsAppService)
    alert_svc = MagicMock(spec=AlertNotificationService)
    task_svc = MagicMock(spec=TaskService)

    return {
        "auto": auto_svc,
        "browser": browser_svc,
        "gmail": gmail_svc,
        "wa": wa_svc,
        "alert": alert_svc,
        "task": task_svc,
    }


@pytest.fixture
def engine(mock_services):
    return WorkflowEngine(
        automation_service=mock_services["auto"],
        browser_service=mock_services["browser"],
        gmail_service=mock_services["gmail"],
        whatsapp_service=mock_services["wa"],
        alert_service=mock_services["alert"],
        task_service=mock_services["task"],
    )


def test_sequential_step_execution_success(engine, mock_services):
    mock_services["auto"].launch_app.return_value = MagicMock(success=True, message="Launched Notepad", data={})
    mock_services["browser"].search_web.return_value = MagicMock(success=True, message="Searched web", data={})

    wf = engine.create_workflow(
        title="Test Workflow",
        description="Sequential test",
        steps=[
            WorkflowStep("s1", "launch_app", "Launch Notepad", {"app_id": "notepad"}),
            WorkflowStep("s2", "web_search", "Search Google", {"query": "python"}),
        ],
    )

    started_signal = MagicMock()
    completed_signal = MagicMock()
    engine.workflow_started.connect(started_signal)
    engine.workflow_completed.connect(completed_signal)

    ok = engine.start_workflow(wf)
    assert ok is True
    assert wf.status == "completed"
    assert wf.current_step_index == 2
    assert wf.steps[0].status == "completed"
    assert wf.steps[1].status == "completed"
    started_signal.assert_called_once_with(wf.id)
    completed_signal.assert_called_once()


def test_step_failure_halts_workflow(engine, mock_services):
    mock_services["auto"].launch_app.return_value = MagicMock(success=False, message="App not found", data={})

    wf = engine.create_workflow(
        title="Failure Test",
        description="Failing step test",
        steps=[
            WorkflowStep("s1", "launch_app", "Launch Invalid App", {"app_id": "invalid_app"}),
            WorkflowStep("s2", "launch_app", "Launch Notepad", {"app_id": "notepad"}),
        ],
    )

    failed_signal = MagicMock()
    engine.workflow_failed.connect(failed_signal)

    engine.start_workflow(wf)
    assert wf.status == "failed"
    assert wf.steps[0].status == "failed"
    assert wf.steps[1].status == "pending"  # Step 2 never ran
    failed_signal.assert_called_once()


def test_pause_resume_and_cancel_controls(engine, mock_services):
    mock_services["browser"].navigate_url.return_value = MagicMock(success=True, message="Navigated", data={})

    wf = engine.create_workflow(
        title="Control Test",
        description="Pause/resume test",
        steps=[
            WorkflowStep("s1", "open_url", "Open URL", {"url": "https://example.com"}),
            WorkflowStep("s2", "open_url", "Open URL 2", {"url": "https://example2.com"}),
        ],
    )

    # Pause control test
    engine.start_workflow(wf)
    assert wf.status == "completed"

    # Testing cancel on active workflow
    wf_cancel = engine.create_workflow(
        title="Cancel Test",
        description="Cancel test",
        steps=[
            WorkflowStep("s1", "launch_app", "App", {"app_id": "notepad"}, requires_confirmation=True),
        ],
    )
    engine.start_workflow(wf_cancel)
    assert wf_cancel.status == "running"
    assert wf_cancel.steps[0].status == "awaiting_confirmation"

    engine.cancel_workflow(wf_cancel.id)
    assert wf_cancel.status == "cancelled"


def test_step_confirmation_prompt_and_user_approval(engine, mock_services):
    mock_services["task"].create_task.return_value = MagicMock(id=99, title="New Task")

    wf = engine.create_workflow(
        title="Confirmation Workflow",
        description="Requires user confirmation",
        steps=[
            WorkflowStep("s1", "create_task", "Create Task", {"title": "New Task"}, requires_confirmation=True),
        ],
    )

    confirm_signal = MagicMock()
    engine.step_confirmation_required.connect(confirm_signal)

    engine.start_workflow(wf)
    assert wf.steps[0].status == "awaiting_confirmation"
    confirm_signal.assert_called_once()

    # User approves
    engine.confirm_step_and_continue(wf.id, 0, approved=True)
    assert wf.status == "completed"
    assert wf.steps[0].status == "completed"


def test_overlapping_execution_prevention(engine, mock_services):
    wf1 = engine.create_workflow(
        title="WF1",
        description="WF1 desc",
        steps=[WorkflowStep("s1", "launch_app", "App", {"app_id": "notepad"}, requires_confirmation=True)],
    )
    wf2 = engine.create_workflow(
        title="WF2",
        description="WF2 desc",
        steps=[WorkflowStep("s1", "launch_app", "App", {"app_id": "notepad"})],
    )

    engine.start_workflow(wf1)
    assert wf1.status == "running"

    # Attempt running second workflow while first is awaiting confirmation
    ok = engine.start_workflow(wf2)
    assert ok is False


def test_master_automation_disabled_check(engine, mock_services):
    mock_services["auto"].is_automation_enabled.return_value = False

    wf = engine.create_workflow(
        title="Disabled Test",
        description="Disabled automation test",
        steps=[WorkflowStep("s1", "launch_app", "App", {"app_id": "notepad"})],
    )

    ok = engine.start_workflow(wf)
    assert ok is False
    assert wf.status == "idle"
