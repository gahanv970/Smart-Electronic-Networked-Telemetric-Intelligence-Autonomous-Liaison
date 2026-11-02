"""
Intelligent Workflow Orchestration Engine for S.E.N.T.I.A.L.
Combines desktop automation, browser automation, Gmail, WhatsApp companion,
alert notifications, tasks, and Gemini AI analysis into controlled, multi-step workflows.
Provides step planning, progress signals, pause/resume/cancel state management,
strict parameter validation, and explicit confirmation safeguards.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from alert_service import AlertNotificationService
from automation_service import WindowsAutomationService
from browser_service import BrowserAutomationService
from gmail_service import GmailService
from task_service import TaskService
from whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)


@dataclass
class WorkflowStep:
    id: str
    action_type: str  # "launch_app" | "open_url" | "open_path" | "web_search" | "extract_web_text" | "summarize_content" | "create_task" | "window_management" | "check_alerts"
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    status: str = "pending"  # "pending" | "running" | "completed" | "failed" | "skipped" | "cancelled" | "awaiting_confirmation"
    result_message: str = ""
    data: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Workflow:
    id: str
    title: str
    description: str
    steps: List[WorkflowStep]
    status: str = "idle"  # "idle" | "running" | "paused" | "completed" | "failed" | "cancelled"
    current_step_index: int = 0
    created_at: str = ""
    completed_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "steps": [s.to_dict() for s in self.steps],
            "status": self.status,
            "current_step_index": self.current_step_index,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


class WorkflowEngine(QObject):
    """
    Dedicated Workflow Engine managing multi-step automation orchestration.
    Reuses existing automation, browser, Gmail, WhatsApp, Alert, Task, and Gemini services.
    Enforces non-overlapping runs, pause/resume/cancel controls, step progress, and confirmation checks.
    """

    workflow_started = Signal(str)                                # (workflow_id)
    step_started = Signal(str, int, str)                           # (workflow_id, step_index, step_description)
    step_completed = Signal(str, int, str)                         # (workflow_id, step_index, result_message)
    step_confirmation_required = Signal(str, int, str)            # (workflow_id, step_index, prompt_message)
    workflow_completed = Signal(str, str)                         # (workflow_id, final_message)
    workflow_failed = Signal(str, str)                            # (workflow_id, error_message)
    workflow_paused = Signal(str)                                 # (workflow_id)
    workflow_resumed = Signal(str)                                # (workflow_id)
    workflow_cancelled = Signal(str)                              # (workflow_id)

    def __init__(
        self,
        automation_service: WindowsAutomationService,
        browser_service: BrowserAutomationService,
        gmail_service: GmailService,
        whatsapp_service: WhatsAppService,
        alert_service: AlertNotificationService,
        task_service: TaskService,
        ai_provider=None,
        parent=None,
    ):
        super().__init__(parent)
        self.automation_service = automation_service
        self.browser_service = browser_service
        self.gmail_service = gmail_service
        self.whatsapp_service = whatsapp_service
        self.alert_service = alert_service
        self.task_service = task_service
        self.ai_provider = ai_provider

        self._active_workflow: Optional[Workflow] = None
        self._workflow_history: List[Workflow] = []

    def get_active_workflow(self) -> Optional[Workflow]:
        return self._active_workflow

    def get_workflow_history(self) -> List[Workflow]:
        return list(self._workflow_history)

    # ------------------------------------------------------------------ Preset & Dynamic Workflow Builders

    def get_preset_workflows(self) -> List[Workflow]:
        """Returns standard pre-registered multi-step workflows."""
        return [
            Workflow(
                id="wf_daily_briefing",
                title="🌅 Morning Briefing Workflow",
                description="Checks Alert Center, summarizes unread notifications, and opens development tools",
                steps=[
                    WorkflowStep("step_1", "check_alerts", "Check Alert Center unread notifications"),
                    WorkflowStep("step_2", "launch_app", "Launch File Explorer", {"app_id": "explorer"}),
                    WorkflowStep("step_3", "launch_app", "Launch Notepad", {"app_id": "notepad"}),
                ],
            ),
            Workflow(
                id="wf_research_and_summarize",
                title="🔍 Research & Summarize Workflow",
                description="Searches the web, extracts page text safely, and generates AI summary",
                steps=[
                    WorkflowStep("step_1", "web_search", "Search web for topic", {"query": "S.E.N.T.I.A.L. Python PySide6"}),
                    WorkflowStep("step_2", "extract_web_text", "Extract readable text from HTTPS webpage", {"url": "https://python.org"}),
                ],
            ),
            Workflow(
                id="wf_task_workspace",
                title="📋 Task & Notes Workspace",
                description="Creates a task and opens Notepad for notes",
                steps=[
                    WorkflowStep("step_1", "create_task", "Create workspace task", {"title": "Review Daily Objectives", "priority": "HIGH"}, requires_confirmation=True),
                    WorkflowStep("step_2", "launch_app", "Launch Notepad", {"app_id": "notepad"}),
                ],
            ),
        ]

    def create_workflow(
        self,
        title: str,
        description: str,
        steps: List[WorkflowStep],
    ) -> Workflow:
        """Creates a custom workflow instance."""
        clean_title = (title or "Custom Workflow").strip()
        clean_desc = (description or "").strip()
        wf_id = f"wf_custom_{int(datetime.now().timestamp() * 1000)}"

        wf = Workflow(
            id=wf_id,
            title=clean_title,
            description=clean_desc,
            steps=steps,
        )
        return wf

    # ------------------------------------------------------------------ Orchestration Controls

    def start_workflow(self, workflow: Workflow) -> bool:
        """
        Starts executing a workflow sequentially.
        Blocks overlapping execution if a workflow is already running or paused.
        """
        if self._active_workflow and self._active_workflow.status in ("running", "paused"):
            logger.warning("Attempted to run workflow while another workflow is active.")
            self.workflow_failed.emit(workflow.id, "An active workflow is already running. Please wait or cancel the active workflow.")
            return False

        if not self.automation_service.is_automation_enabled():
            self.workflow_failed.emit(workflow.id, "Master desktop automation is disabled in settings.")
            return False

        self._active_workflow = workflow
        self._active_workflow.status = "running"
        self._active_workflow.current_step_index = 0

        self.workflow_started.emit(workflow.id)
        logger.info(f"Workflow '{workflow.title}' ({workflow.id}) started.")

        self._execute_current_step()
        return True

    def pause_workflow(self, workflow_id: str) -> bool:
        if self._active_workflow and self._active_workflow.id == workflow_id and self._active_workflow.status == "running":
            self._active_workflow.status = "paused"
            self.workflow_paused.emit(workflow_id)
            logger.info(f"Workflow '{workflow_id}' paused.")
            return True
        return False

    def resume_workflow(self, workflow_id: str) -> bool:
        if self._active_workflow and self._active_workflow.id == workflow_id and self._active_workflow.status == "paused":
            self._active_workflow.status = "running"
            self.workflow_resumed.emit(workflow_id)
            logger.info(f"Workflow '{workflow_id}' resumed.")
            self._execute_current_step()
            return True
        return False

    def cancel_workflow(self, workflow_id: str) -> bool:
        if self._active_workflow and self._active_workflow.id == workflow_id:
            self._active_workflow.status = "cancelled"
            if 0 <= self._active_workflow.current_step_index < len(self._active_workflow.steps):
                self._active_workflow.steps[self._active_workflow.current_step_index].status = "cancelled"
            self.workflow_cancelled.emit(workflow_id)
            self._finish_workflow("Workflow cancelled by user.", success=False)
            logger.info(f"Workflow '{workflow_id}' cancelled.")
            return True
        return False

    def confirm_step_and_continue(self, workflow_id: str, step_index: int, approved: bool) -> None:
        """
        Receives user approval or rejection for confirmation-required step and continues execution.
        """
        if not self._active_workflow or self._active_workflow.id != workflow_id:
            return

        if self._active_workflow.current_step_index != step_index:
            return

        step = self._active_workflow.steps[step_index]
        if not approved:
            step.status = "cancelled"
            step.result_message = "Step cancelled by user."
            self.workflow_failed.emit(workflow_id, f"Step #{step_index + 1} '{step.description}' was declined by user.")
            self._finish_workflow("Workflow halted due to user rejection.", success=False)
            return

        # User approved: execute step with user_confirmed=True
        step.status = "running"
        res_msg, res_data, success = self._run_step_action(step, user_confirmed=True)

        if success:
            step.status = "completed"
            step.result_message = res_msg
            step.data = res_data
            self.step_completed.emit(workflow_id, step_index, res_msg)
            self._advance_to_next_step()
        else:
            step.status = "failed"
            step.result_message = res_msg
            self.workflow_failed.emit(workflow_id, f"Step #{step_index + 1} failed: {res_msg}")
            self._finish_workflow(f"Workflow failed at step #{step_index + 1}: {res_msg}", success=False)

    # ------------------------------------------------------------------ Step Execution Pipeline

    def _execute_current_step(self) -> None:
        if not self._active_workflow or self._active_workflow.status != "running":
            return

        if not self.automation_service.is_automation_enabled():
            self._active_workflow.status = "failed"
            self.workflow_failed.emit(self._active_workflow.id, "Master desktop automation was disabled during execution.")
            self._finish_workflow("Workflow halted: Desktop automation disabled in settings.", success=False)
            return

        idx = self._active_workflow.current_step_index
        if idx >= len(self._active_workflow.steps):
            self._finish_workflow("All workflow steps completed successfully.", success=True)
            return

        step = self._active_workflow.steps[idx]
        step.status = "running"
        self.step_started.emit(self._active_workflow.id, idx, step.description)

        # Check if step requires confirmation
        if step.requires_confirmation:
            step.status = "awaiting_confirmation"
            prompt = f"Confirm step #{idx + 1}: '{step.description}'?"
            self.step_confirmation_required.emit(self._active_workflow.id, idx, prompt)
            return

        res_msg, res_data, success = self._run_step_action(step, user_confirmed=False)

        if success:
            step.status = "completed"
            step.result_message = res_msg
            step.data = res_data
            self.step_completed.emit(self._active_workflow.id, idx, res_msg)
            self._advance_to_next_step()
        else:
            step.status = "failed"
            step.result_message = res_msg
            self.workflow_failed.emit(self._active_workflow.id, f"Step #{idx + 1} failed: {res_msg}")
            self._finish_workflow(f"Workflow failed at step #{idx + 1}: {res_msg}", success=False)

    def _advance_to_next_step(self) -> None:
        if not self._active_workflow:
            return

        self._active_workflow.current_step_index += 1
        if self._active_workflow.current_step_index >= len(self._active_workflow.steps):
            self._finish_workflow("Workflow completed successfully.", success=True)
        else:
            if self._active_workflow.status == "running":
                self._execute_current_step()

    def _finish_workflow(self, final_message: str, success: bool) -> None:
        if not self._active_workflow:
            return

        self._active_workflow.completed_at = datetime.now().isoformat()
        if success:
            self._active_workflow.status = "completed"
            self.workflow_completed.emit(self._active_workflow.id, final_message)
        else:
            if self._active_workflow.status != "cancelled":
                self._active_workflow.status = "failed"

        self._workflow_history.append(self._active_workflow)
        if len(self._workflow_history) > 50:
            self._workflow_history.pop(0)

        logger.info(f"Finished workflow '{self._active_workflow.id}': success={success}, msg='{final_message}'")

    # ------------------------------------------------------------------ Step Executors

    def _run_step_action(
        self,
        step: WorkflowStep,
        user_confirmed: bool = False,
    ) -> Tuple[str, Optional[Dict[str, Any]], bool]:
        """
        Executes a single step using standard underlying services.
        Returns tuple of (result_message, data, success).
        """
        act = step.action_type
        params = step.parameters or {}

        try:
            if act == "launch_app":
                res = self.automation_service.launch_app(params.get("app_id", ""), user_confirmed=user_confirmed)
                return res.message, res.data, res.success

            elif act == "open_url":
                res = self.browser_service.navigate_url(params.get("url", ""), user_confirmed=user_confirmed)
                return res.message, res.data, res.success

            elif act == "open_path":
                res = self.automation_service.open_path(params.get("path", ""), user_confirmed=user_confirmed)
                return res.message, res.data, res.success

            elif act == "web_search":
                res = self.browser_service.search_web(params.get("query", ""), user_confirmed=user_confirmed)
                return res.message, res.data, res.success

            elif act == "extract_web_text":
                res = self.browser_service.extract_page_text(params.get("url", ""))
                return res.message, res.data, res.success

            elif act == "create_task":
                if not user_confirmed:
                    return (
                        "Creating a task requires explicit user confirmation.",
                        None,
                        False,
                    )
                title = params.get("title", "")
                if not title:
                    return "Task title cannot be empty.", None, False
                task = self.task_service.create_task(
                    title=title,
                    description=params.get("description", ""),
                    priority=params.get("priority", "MEDIUM").upper(),
                    due_date=params.get("due_date"),
                )
                return f"Task #{task.id} '{task.title}' created successfully.", {"task_id": task.id}, True

            elif act == "check_alerts":
                unread = self.alert_service.get_unread_count()
                critical = self.alert_service.get_critical_count()
                msg = f"Alert Center: {unread} unread notifications ({critical} Critical/High)."
                return msg, {"unread": unread, "critical": critical}, True

            elif act == "window_management":
                res = self.automation_service.window_management(
                    params.get("action", ""), params.get("app_id", ""), user_confirmed=user_confirmed
                )
                return res.message, res.data, res.success

            else:
                return f"Unsupported step action_type '{act}'.", None, False

        except Exception as e:
            logger.error(f"Error executing step action '{act}': {e}")
            return f"Step execution error: {e}", None, False
