"""
Anime Cyberpunk UI Views for S.E.N.T.I.A.L.
Contains dedicated, fully-functional view widgets for:
- Task Management (TaskManagementWidget)
- Memory & Personalization (MemoryManagementWidget)
- Gmail Integration (EmailManagementWidget)
- Windows Automation (AutomationManagementWidget)
"""

import asyncio
from datetime import datetime
import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
import shutil
from PySide6.QtCore import QTimer, QTime, QDate
from qasync import asyncSlot

from automation_service import APPROVED_APPS, PRESET_WORKFLOWS, WindowsAutomationService
from browser_service import BrowserAutomationService
from workflow_engine import Workflow, WorkflowEngine, WorkflowStep
from gmail_service import EmailDetail, EmailSummary, GmailService, generate_reply_draft, summarize_and_classify_email
from alert_service import AlertNotification, AlertNotificationService
from whatsapp_service import ImportantContact, WhatsAppMessage, WhatsAppNotificationListener, WhatsAppService
from gui_components import LiquidGlassCard
from memory_service import MemoryService
from task_service import Task, TaskService
from utils import show_error_message, show_info_message

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 0. MAIN DASHBOARD WIDGET (S.E.N.T.I.A.L. HOME)
# ----------------------------------------------------------------------

class MainDashboardWidget(QWidget):
    """
    Polished Home Dashboard for S.E.N.T.I.A.L.
    Features core status, voice toggle, digital clock/date, quick action tiles,
    live connection indicators (Gemini & ElevenLabs), recent activity, and workflow status.
    Lightweight and optimized for Intel HD Graphics 530 at 1366x768.
    """

    quick_action_triggered = Signal(int)  # Emits target tab page index

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._setup_ui()
        self._clock_timer.start(1000)
        self._update_clock()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # 1. Top Identity & Live Clock Banner Card
        top_card = LiquidGlassCard(self, corner_radius=18, border_color=QColor(255, 42, 109, 150))
        top_layout = QHBoxLayout(top_card)
        top_layout.setContentsMargins(20, 14, 20, 14)

        identity_box = QVBoxLayout()
        hdr_title = QLabel("S.E.N.T.I.A.L.")
        hdr_title.setObjectName("hdrAnimeTitle")
        hdr_title.setStyleSheet("font-size: 24px; font-weight: bold; color: #FF2A6D; letter-spacing: 2px;")

        hdr_sub = QLabel("Smart Electronic Networked Telemetric Intelligence & Autonomous Liaison")
        hdr_sub.setStyleSheet("font-size: 11px; color: #05D9E8; font-weight: 600;")

        identity_box.addWidget(hdr_title)
        identity_box.addWidget(hdr_sub)
        top_layout.addLayout(identity_box)

        top_layout.addStretch()

        # Clock & Date Box
        clock_box = QVBoxLayout()
        clock_box.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.time_lbl = QLabel("00:00:00")
        self.time_lbl.setStyleSheet("font-size: 22px; font-weight: bold; color: #05D9E8; font-family: 'Consolas', monospace;")
        self.date_lbl = QLabel("2025-01-01")
        self.date_lbl.setStyleSheet("font-size: 11px; color: #A0AEC0; font-weight: 600;")
        clock_box.addWidget(self.time_lbl, alignment=Qt.AlignRight)
        clock_box.addWidget(self.date_lbl, alignment=Qt.AlignRight)
        top_layout.addLayout(clock_box)

        layout.addWidget(top_card)

        # 2. Status Indicators Row Card (Gemini AI, ElevenLabs TTS, Voice Activation, Active Workflow)
        status_card = LiquidGlassCard(self, corner_radius=16, border_color=QColor(5, 217, 232, 120))
        status_layout = QHBoxLayout(status_card)
        status_layout.setContentsMargins(16, 10, 16, 10)
        status_layout.setSpacing(12)

        # Gemini Indicator
        self.gemini_ind = QLabel("● Gemini AI: Checking...")
        self.gemini_ind.setStyleSheet("color: #05D9E8; font-weight: bold; font-size: 12px;")
        status_layout.addWidget(self.gemini_ind)

        # ElevenLabs Indicator
        self.eleven_ind = QLabel("● ElevenLabs TTS: Ready")
        self.eleven_ind.setStyleSheet("color: #9D4EDD; font-weight: bold; font-size: 12px;")
        status_layout.addWidget(self.eleven_ind)

        # Voice Activation Toggle Status
        self.voice_act_ind = QLabel("● Voice Input: Idle")
        self.voice_act_ind.setStyleSheet("color: #A0AEC0; font-size: 12px;")
        status_layout.addWidget(self.voice_act_ind)

        # Workflow Status
        self.wf_ind = QLabel("● Workflow: Standby")
        self.wf_ind.setStyleSheet("color: #A0AEC0; font-size: 12px;")
        status_layout.addWidget(self.wf_ind)

        status_layout.addStretch()
        layout.addWidget(status_card)

        # 3. Middle Section: Quick Action Tiles & Notification Summary
        mid_layout = QHBoxLayout()
        mid_layout.setSpacing(12)

        # Quick Actions Card
        qa_card = LiquidGlassCard(self, corner_radius=16)
        qa_layout = QVBoxLayout(qa_card)
        qa_layout.setContentsMargins(16, 14, 16, 14)
        qa_layout.setSpacing(10)

        qa_title = QLabel("⚡ QUICK ACTIONS")
        qa_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #FF2A6D;")
        qa_layout.addWidget(qa_title)

        grid = QGridLayout()
        grid.setSpacing(8)

        btn_chat = QPushButton("💬 Open Voice Chat")
        btn_chat.setObjectName("btnPrimary")
        btn_chat.clicked.connect(lambda: self.quick_action_triggered.emit(1))
        grid.addWidget(btn_chat, 0, 0)

        btn_alerts = QPushButton("🔔 Notification Center")
        btn_alerts.clicked.connect(lambda: self.quick_action_triggered.emit(2))
        grid.addWidget(btn_alerts, 0, 1)

        btn_wf = QPushButton("⚡ Execute Workflows")
        btn_wf.clicked.connect(lambda: self.quick_action_triggered.emit(3))
        grid.addWidget(btn_wf, 1, 0)

        btn_email = QPushButton("📧 Gmail Control")
        btn_email.clicked.connect(lambda: self.quick_action_triggered.emit(4))
        grid.addWidget(btn_email, 1, 1)

        btn_wa = QPushButton("📱 WhatsApp Companion")
        btn_wa.clicked.connect(lambda: self.quick_action_triggered.emit(5))
        grid.addWidget(btn_wa, 2, 0)

        btn_auto = QPushButton("⚙️ Windows Automation")
        btn_auto.clicked.connect(lambda: self.quick_action_triggered.emit(6))
        grid.addWidget(btn_auto, 2, 1)

        qa_layout.addLayout(grid)
        mid_layout.addWidget(qa_card, stretch=1)

        # Notification & Intelligence Summary Card
        sum_card = LiquidGlassCard(self, corner_radius=16)
        sum_layout = QVBoxLayout(sum_card)
        sum_layout.setContentsMargins(16, 14, 16, 14)
        sum_layout.setSpacing(8)

        sum_title = QLabel("📊 INTELLIGENCE SUMMARY")
        sum_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #05D9E8;")
        sum_layout.addWidget(sum_title)

        self.unread_summary_lbl = QLabel("• 0 Unread Notifications")
        self.unread_summary_lbl.setStyleSheet("color: #E2E8F0; font-size: 12px;")
        sum_layout.addWidget(self.unread_summary_lbl)

        self.critical_summary_lbl = QLabel("• 0 Critical Urgent Alerts")
        self.critical_summary_lbl.setStyleSheet("color: #FF2A6D; font-size: 12px; font-weight: bold;")
        sum_layout.addWidget(self.critical_summary_lbl)

        self.task_summary_lbl = QLabel("• 0 Pending Tasks")
        self.task_summary_lbl.setStyleSheet("color: #A0AEC0; font-size: 12px;")
        sum_layout.addWidget(self.task_summary_lbl)

        self.memory_summary_lbl = QLabel("• Long-Term Memory Active")
        self.memory_summary_lbl.setStyleSheet("color: #7FFFD4; font-size: 12px;")
        sum_layout.addWidget(self.memory_summary_lbl)

        sum_layout.addStretch()
        mid_layout.addWidget(sum_card, stretch=1)

        layout.addLayout(mid_layout)

        # 4. Activity Log Feed Card
        log_card = LiquidGlassCard(self, corner_radius=16)
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(16, 12, 16, 12)
        log_layout.setSpacing(6)

        log_title = QLabel("📡 SYSTEM RECENT ACTIVITY LOG")
        log_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #9D4EDD;")
        log_layout.addWidget(log_title)

        self.activity_log_display = QTextEdit()
        self.activity_log_display.setReadOnly(True)
        self.activity_log_display.setFixedHeight(110)
        self.activity_log_display.setStyleSheet("background-color: rgba(11, 12, 16, 0.9); font-family: Consolas, monospace; font-size: 11px; color: #05D9E8; border: 1px solid rgba(5, 217, 232, 0.2);")
        self.activity_log_display.setText(f"[{datetime.now().strftime('%H:%M:%S')}] S.E.N.T.I.A.L. Core System Initialized.\n[{datetime.now().strftime('%H:%M:%S')}] Anime Cyberpunk Interface Loaded.")
        log_layout.addWidget(self.activity_log_display)

        layout.addWidget(log_card, stretch=1)

    def _update_clock(self):
        now_time = QTime.currentTime().toString("hh:mm:ss")
        now_date = QDate.currentDate().toString("yyyy-MM-dd dddd")
        self.time_lbl.setText(now_time)
        self.date_lbl.setText(now_date)

    def append_activity(self, text: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.activity_log_display.append(f"[{timestamp}] {text}")

    def update_indicators(self, gemini_status: str, voice_status: str):
        self.gemini_ind.setText(f"● Gemini AI: {gemini_status}")
        self.eleven_ind.setText(f"● ElevenLabs: {voice_status}")


# ----------------------------------------------------------------------
# 1. TASK MANAGEMENT WIDGET
# ----------------------------------------------------------------------

class TaskManagementWidget(QWidget):
    """
    Dedicated view for managing persistent tasks (CRUD, priority, due dates, filters, search).
    """

    task_count_updated = Signal(int, int)  # (pending_count, completed_count)

    def __init__(self, task_service: TaskService, parent=None):
        super().__init__(parent)
        self.task_service = task_service
        self._setup_ui()
        self.refresh_tasks()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        # Header Card
        header_card = LiquidGlassCard(self, corner_radius=18)
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(18, 12, 18, 12)

        title_box = QVBoxLayout()
        title = QLabel("<h2 style='color: #00C8FF; margin: 0;'>Task Management</h2>")
        self.stats_label = QLabel("Track and organize your personal and development tasks")
        self.stats_label.setStyleSheet("color: #A0AEC0; font-size: 13px;")
        title_box.addWidget(title)
        title_box.addWidget(self.stats_label)
        header_layout.addLayout(title_box)

        header_layout.addStretch()

        self.refresh_btn = QPushButton("🔄 Refresh")
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.refresh_tasks)
        header_layout.addWidget(self.refresh_btn)

        layout.addWidget(header_card)

        # Create Task Card
        create_card = LiquidGlassCard(self, corner_radius=18)
        create_layout = QVBoxLayout(create_card)
        create_layout.setContentsMargins(18, 14, 18, 14)
        create_layout.setSpacing(10)

        create_title = QLabel("<b style='color: #7FFFD4; font-size: 14px;'>Create New Task</b>")
        create_layout.addWidget(create_title)

        row1 = QHBoxLayout()
        row1.setSpacing(10)
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Task title (required)...")
        self.title_input.setFixedHeight(36)
        row1.addWidget(self.title_input, stretch=3)

        self.priority_combo = QComboBox()
        self.priority_combo.addItems(["MEDIUM", "LOW", "HIGH", "CRITICAL"])
        self.priority_combo.setFixedHeight(36)
        row1.addWidget(self.priority_combo, stretch=1)

        self.due_date_input = QLineEdit()
        self.due_date_input.setPlaceholderText("Due Date (YYYY-MM-DD)")
        self.due_date_input.setFixedHeight(36)
        row1.addWidget(self.due_date_input, stretch=1)

        self.add_btn = QPushButton("➕ Add Task")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setFixedHeight(36)
        self.add_btn.clicked.connect(self._create_task)
        row1.addWidget(self.add_btn, stretch=1)

        create_layout.addLayout(row1)

        self.desc_input = QLineEdit()
        self.desc_input.setPlaceholderText("Optional description or details...")
        self.desc_input.setFixedHeight(34)
        create_layout.addWidget(self.desc_input)

        layout.addWidget(create_card)

        # Filter and Search Bar
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Search tasks...")
        self.search_input.setFixedHeight(34)
        self.search_input.textChanged.connect(self.refresh_tasks)
        filter_bar.addWidget(self.search_input, stretch=2)

        self.status_filter = QComboBox()
        self.status_filter.addItems(["All Tasks", "Pending Only", "Completed Only"])
        self.status_filter.setFixedHeight(34)
        self.status_filter.currentIndexChanged.connect(self.refresh_tasks)
        filter_bar.addWidget(self.status_filter, stretch=1)

        self.priority_filter = QComboBox()
        self.priority_filter.addItems(["All Priorities", "CRITICAL", "HIGH", "MEDIUM", "LOW"])
        self.priority_filter.setFixedHeight(34)
        self.priority_filter.currentIndexChanged.connect(self.refresh_tasks)
        filter_bar.addWidget(self.priority_filter, stretch=1)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Newest First", "Oldest First", "By Priority", "By Due Date"])
        self.sort_combo.setFixedHeight(34)
        self.sort_combo.currentIndexChanged.connect(self.refresh_tasks)
        filter_bar.addWidget(self.sort_combo, stretch=1)

        layout.addLayout(filter_bar)

        # Scroll Area for Task Items
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.task_list_container = QWidget()
        self.task_list_layout = QVBoxLayout(self.task_list_container)
        self.task_list_layout.setContentsMargins(0, 0, 0, 0)
        self.task_list_layout.setSpacing(8)
        self.task_list_layout.addStretch()
        self.scroll_area.setWidget(self.task_list_container)

        layout.addWidget(self.scroll_area, stretch=1)

    def _create_task(self):
        title = self.title_input.text().strip()
        if not title:
            show_error_message("Validation Error", "Task title cannot be empty.")
            return

        priority = self.priority_combo.currentText()
        due_date = self.due_date_input.text().strip() or None
        desc = self.desc_input.text().strip()

        try:
            self.task_service.create_task(
                title=title, description=desc, priority=priority, due_date=due_date
            )
            self.title_input.clear()
            self.desc_input.clear()
            self.due_date_input.clear()
            self.refresh_tasks()
        except Exception as e:
            show_error_message("Task Error", str(e))

    def refresh_tasks(self):
        # Clear existing items in layout
        while self.task_list_layout.count() > 1:
            child = self.task_list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        status_text = self.status_filter.currentText()
        status_filter = None
        if status_text == "Pending Only":
            status_filter = "pending"
        elif status_text == "Completed Only":
            status_filter = "completed"

        pri_text = self.priority_filter.currentText()
        priority_filter = None if pri_text == "All Priorities" else pri_text

        search = self.search_input.text().strip() or None

        sort_map = {
            "Newest First": "created_desc",
            "Oldest First": "created_asc",
            "By Priority": "priority",
            "By Due Date": "due_date",
        }
        sort_by = sort_map.get(self.sort_combo.currentText(), "created_desc")

        tasks = self.task_service.get_all_tasks(
            status_filter=status_filter,
            priority_filter=priority_filter,
            search_query=search,
            sort_by=sort_by,
        )

        all_tasks = self.task_service.get_all_tasks()
        pending_count = sum(1 for t in all_tasks if t.status == "pending")
        completed_count = sum(1 for t in all_tasks if t.status == "completed")
        self.stats_label.setText(f"{pending_count} Pending | {completed_count} Completed | Total: {len(all_tasks)}")
        self.task_count_updated.emit(pending_count, completed_count)

        if not tasks:
            empty_lbl = QLabel("<p style='color: #718096; text-align: center; margin: 30px;'>No tasks found matching criteria.</p>")
            empty_lbl.setAlignment(Qt.AlignCenter)
            self.task_list_layout.insertWidget(0, empty_lbl)
            return

        for task in tasks:
            card = self._create_task_item_widget(task)
            self.task_list_layout.insertWidget(self.task_list_layout.count() - 1, card)

    def _create_task_item_widget(self, task: Task) -> QWidget:
        item = LiquidGlassCard(self.task_list_container, corner_radius=14)
        item_layout = QHBoxLayout(item)
        item_layout.setContentsMargins(14, 10, 14, 10)
        item_layout.setSpacing(12)

        # Checkbox toggle
        chk = QCheckBox()
        chk.setChecked(task.status == "completed")
        chk.toggled.connect(lambda checked, tid=task.id: self._toggle_status(tid))
        item_layout.addWidget(chk)

        # Priority Badge
        pri_colors = {
            "CRITICAL": "#FF4444",
            "HIGH": "#FFAA00",
            "MEDIUM": "#00C8FF",
            "LOW": "#888888",
        }
        color = pri_colors.get(task.priority, "#00C8FF")
        badge = QLabel(f" {task.priority} ")
        badge.setStyleSheet(f"""
            background-color: rgba(255, 255, 255, 10);
            color: {color};
            border: 1px solid {color};
            border-radius: 6px;
            font-size: 11px;
            font-weight: bold;
            padding: 3px 6px;
        """)
        item_layout.addWidget(badge)

        # Task Text Box
        content_box = QVBoxLayout()
        content_box.setSpacing(2)

        title_style = "color: #718096; text-decoration: line-through;" if task.status == "completed" else "color: #F0F4F8; font-weight: bold;"
        title_lbl = QLabel(f"<span style='{title_style} font-size: 13px;'>{task.title}</span>")
        content_box.addWidget(title_lbl)

        if task.description:
            desc_lbl = QLabel(f"<span style='color: #A0AEC0; font-size: 11px;'>{task.description}</span>")
            content_box.addWidget(desc_lbl)

        item_layout.addLayout(content_box, stretch=1)

        # Due Date Badge
        if task.due_date:
            due_lbl = QLabel(f"📅 {task.due_date}")
            due_lbl.setStyleSheet("color: #7FFFD4; font-size: 11px; background: rgba(0, 200, 255, 20); padding: 4px 8px; border-radius: 6px;")
            item_layout.addWidget(due_lbl)

        # Edit button
        edit_btn = QPushButton("✏️")
        edit_btn.setFixedSize(32, 32)
        edit_btn.setCursor(Qt.PointingHandCursor)
        edit_btn.setToolTip("Edit Task")
        edit_btn.clicked.connect(lambda _, t=task: self._edit_task_dialog(t))
        item_layout.addWidget(edit_btn)

        # Delete button
        del_btn = QPushButton("🗑️")
        del_btn.setFixedSize(32, 32)
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setToolTip("Delete Task")
        del_btn.setStyleSheet("color: #FF6B6B;")
        del_btn.clicked.connect(lambda _, tid=task.id: self._confirm_delete(tid))
        item_layout.addWidget(del_btn)

        return item

    def _toggle_status(self, task_id: int):
        self.task_service.toggle_task_status(task_id)
        self.refresh_tasks()

    def _confirm_delete(self, task_id: int):
        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            "Are you sure you want to delete this task?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.task_service.delete_task(task_id)
            self.refresh_tasks()

    def _edit_task_dialog(self, task: Task):
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Edit Task #{task.id}")
        dialog.setFixedWidth(400)
        d_layout = QVBoxLayout(dialog)
        d_layout.setSpacing(10)

        d_layout.addWidget(QLabel("Title:"))
        title_in = QLineEdit(task.title)
        d_layout.addWidget(title_in)

        d_layout.addWidget(QLabel("Description:"))
        desc_in = QLineEdit(task.description)
        d_layout.addWidget(desc_in)

        d_layout.addWidget(QLabel("Priority:"))
        pri_combo = QComboBox()
        pri_combo.addItems(["LOW", "MEDIUM", "HIGH", "CRITICAL"])
        pri_combo.setCurrentText(task.priority)
        d_layout.addWidget(pri_combo)

        d_layout.addWidget(QLabel("Due Date (YYYY-MM-DD):"))
        due_in = QLineEdit(task.due_date or "")
        d_layout.addWidget(due_in)

        btn_box = QHBoxLayout()
        save_btn = QPushButton("Save Changes")
        save_btn.clicked.connect(dialog.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        btn_box.addWidget(save_btn)
        btn_box.addWidget(cancel_btn)
        d_layout.addLayout(btn_box)

        if dialog.exec() == QDialog.Accepted:
            new_title = title_in.text().strip()
            if new_title:
                self.task_service.update_task(
                    task.id,
                    title=new_title,
                    description=desc_in.text().strip(),
                    priority=pri_combo.currentText(),
                    due_date=due_in.text().strip() or None,
                )
                self.refresh_tasks()


# ----------------------------------------------------------------------
# 2. MEMORY & PERSONALIZATION WIDGET
# ----------------------------------------------------------------------

class MemoryManagementWidget(QWidget):
    """
    Dedicated view for configuring long-term user preferences,
    inspecting remembered context, adding facts, and managing memories.
    """

    def __init__(self, memory_service: MemoryService, parent=None):
        super().__init__(parent)
        self.memory_service = memory_service
        self._setup_ui()
        self.refresh_memories()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        # Header Card
        header_card = LiquidGlassCard(self, corner_radius=18)
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(18, 12, 18, 12)

        title_box = QVBoxLayout()
        title = QLabel("<h2 style='color: #00C8FF; margin: 0;'>Memory & Personalization</h2>")
        desc = QLabel("User preferences and contextual memories injected into AI responses")
        desc.setStyleSheet("color: #A0AEC0; font-size: 13px;")
        title_box.addWidget(title)
        title_box.addWidget(desc)
        header_layout.addLayout(title_box)

        header_layout.addStretch()

        self.memory_toggle = QCheckBox("Enable Long-Term Memory")
        self.memory_toggle.setChecked(self.memory_service.is_memory_enabled())
        self.memory_toggle.toggled.connect(self._toggle_memory)
        header_layout.addWidget(self.memory_toggle)

        layout.addWidget(header_card)

        # Preferences Card
        pref_card = LiquidGlassCard(self, corner_radius=18)
        pref_layout = QVBoxLayout(pref_card)
        pref_layout.setContentsMargins(18, 14, 18, 14)
        pref_layout.setSpacing(10)

        pref_title = QLabel("<b style='color: #7FFFD4; font-size: 14px;'>User Preferences</b>")
        pref_layout.addWidget(pref_title)

        prefs = self.memory_service.get_preferences()
        row = QHBoxLayout()
        row.setSpacing(10)

        self.user_name_input = QLineEdit(prefs.get("user_name", ""))
        self.user_name_input.setPlaceholderText("Your Name (e.g. Tony)")
        self.user_name_input.setFixedHeight(34)
        row.addWidget(self.user_name_input, stretch=1)

        self.tone_input = QLineEdit(prefs.get("preferred_tone", ""))
        self.tone_input.setPlaceholderText("Tone (e.g. Concise, technical, witty)")
        self.tone_input.setFixedHeight(34)
        row.addWidget(self.tone_input, stretch=2)

        self.lang_input = QLineEdit(prefs.get("code_language_preference", ""))
        self.lang_input.setPlaceholderText("Preferred Language (e.g. Python)")
        self.lang_input.setFixedHeight(34)
        row.addWidget(self.lang_input, stretch=1)

        save_pref_btn = QPushButton("Save Preferences")
        save_pref_btn.setFixedHeight(34)
        save_pref_btn.setCursor(Qt.PointingHandCursor)
        save_pref_btn.clicked.connect(self._save_preferences)
        row.addWidget(save_pref_btn, stretch=1)

        pref_layout.addLayout(row)
        layout.addWidget(pref_card)

        # Add Memory Card
        add_card = LiquidGlassCard(self, corner_radius=18)
        add_layout = QHBoxLayout(add_card)
        add_layout.setContentsMargins(18, 12, 18, 12)
        add_layout.setSpacing(10)

        self.mem_content_input = QLineEdit()
        self.mem_content_input.setPlaceholderText("Add a fact or context (e.g. 'Working on Windows AI desktop project')...")
        self.mem_content_input.setFixedHeight(36)
        add_layout.addWidget(self.mem_content_input, stretch=3)

        self.category_combo = QComboBox()
        self.category_combo.addItems(["fact", "preference", "project", "instruction"])
        self.category_combo.setFixedHeight(36)
        add_layout.addWidget(self.category_combo, stretch=1)

        add_mem_btn = QPushButton("➕ Add Memory")
        add_mem_btn.setFixedHeight(36)
        add_mem_btn.setCursor(Qt.PointingHandCursor)
        add_mem_btn.clicked.connect(self._add_memory)
        add_layout.addWidget(add_mem_btn, stretch=1)

        layout.addWidget(add_card)

        # Action Bar (Search & Clear All)
        bar = QHBoxLayout()
        self.search_mem_input = QLineEdit()
        self.search_mem_input.setPlaceholderText("🔍 Search stored memories...")
        self.search_mem_input.setFixedHeight(34)
        self.search_mem_input.textChanged.connect(self.refresh_memories)
        bar.addWidget(self.search_mem_input, stretch=3)

        self.clear_all_btn = QPushButton("🗑️ Clear All Memories")
        self.clear_all_btn.setFixedHeight(34)
        self.clear_all_btn.setCursor(Qt.PointingHandCursor)
        self.clear_all_btn.setStyleSheet("color: #FF6B6B;")
        self.clear_all_btn.clicked.connect(self._clear_all_memories)
        bar.addWidget(self.clear_all_btn, stretch=1)

        layout.addLayout(bar)

        # Scroll Area for Memory List
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.mem_list_container = QWidget()
        self.mem_list_layout = QVBoxLayout(self.mem_list_container)
        self.mem_list_layout.setContentsMargins(0, 0, 0, 0)
        self.mem_list_layout.setSpacing(8)
        self.mem_list_layout.addStretch()
        self.scroll_area.setWidget(self.mem_list_container)

        layout.addWidget(self.scroll_area, stretch=1)

    def _toggle_memory(self, checked: bool):
        self.memory_service.set_memory_enabled(checked)
        show_info_message("Memory Setting", f"Memory context injection is now {'enabled' if checked else 'disabled'}.")

    def _save_preferences(self):
        updates = {
            "user_name": self.user_name_input.text().strip(),
            "preferred_tone": self.tone_input.text().strip(),
            "code_language_preference": self.lang_input.text().strip(),
        }
        self.memory_service.save_preferences(updates)
        show_info_message("Preferences Saved", "Your AI personalization settings have been updated.")

    def _add_memory(self):
        content = self.mem_content_input.text().strip()
        category = self.category_combo.currentText()
        if not content:
            show_error_message("Validation Error", "Memory content cannot be blank.")
            return

        try:
            self.memory_service.add_memory(content, category=category)
            self.mem_content_input.clear()
            self.refresh_memories()
        except Exception as e:
            show_error_message("Security Rejection", str(e))

    def refresh_memories(self):
        while self.mem_list_layout.count() > 1:
            child = self.mem_list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        search = self.search_mem_input.text().strip() or None
        memories = self.memory_service.get_all_memories(search_query=search)

        if not memories:
            empty = QLabel("<p style='color: #718096; text-align: center; margin: 30px;'>No stored memories found.</p>")
            empty.setAlignment(Qt.AlignCenter)
            self.mem_list_layout.insertWidget(0, empty)
            return

        for mem in memories:
            item = LiquidGlassCard(self.mem_list_container, corner_radius=14)
            i_layout = QHBoxLayout(item)
            i_layout.setContentsMargins(14, 10, 14, 10)
            i_layout.setSpacing(10)

            cat_badge = QLabel(f"[{mem.category.upper()}]")
            cat_badge.setStyleSheet("color: #7FFFD4; font-weight: bold; font-size: 11px;")
            i_layout.addWidget(cat_badge)

            content_lbl = QLabel(mem.content)
            content_lbl.setStyleSheet("color: #E2E8F0; font-size: 13px;")
            content_lbl.setWordWrap(True)
            i_layout.addWidget(content_lbl, stretch=1)

            date_lbl = QLabel(mem.created_at[:10])
            date_lbl.setStyleSheet("color: #718096; font-size: 11px;")
            i_layout.addWidget(date_lbl)

            del_btn = QPushButton("🗑️")
            del_btn.setFixedSize(30, 30)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setStyleSheet("color: #FF6B6B;")
            del_btn.clicked.connect(lambda _, mid=mem.id: self._delete_memory(mid))
            i_layout.addWidget(del_btn)

            self.mem_list_layout.insertWidget(self.mem_list_layout.count() - 1, item)

    def _delete_memory(self, memory_id: int):
        self.memory_service.delete_memory(memory_id)
        self.refresh_memories()

    def _clear_all_memories(self):
        reply = QMessageBox.warning(
            self,
            "Clear All Memories",
            "Are you sure you want to delete ALL remembered context? This action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.memory_service.clear_all_memories()
            self.refresh_memories()
            show_info_message("Cleared", "All stored memories have been cleared.")


# ----------------------------------------------------------------------
# 3. GMAIL INTEGRATION WIDGET
# ----------------------------------------------------------------------

class EmailManagementWidget(QWidget):
    """
    Dedicated view for Gmail integration:
    OAuth authorization, mailbox inbox, pagination, message viewing, draft composing,
    and user-confirmed message dispatching.
    """

    def __init__(self, gmail_service: GmailService, parent=None):
        super().__init__(parent)
        self.gmail_service = gmail_service
        self._current_page_token: Optional[str] = None
        self._next_page_token: Optional[str] = None
        self._page_tokens_history: List[Optional[str]] = []
        self._active_draft_id: Optional[str] = None
        self._setup_ui()
        self.update_auth_status()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        # Header Card with Status
        header_card = LiquidGlassCard(self, corner_radius=18)
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(18, 12, 18, 12)

        title_box = QVBoxLayout()
        title = QLabel("<h2 style='color: #00C8FF; margin: 0;'>Gmail Integration</h2>")
        self.status_label = QLabel("Status: Checking authentication...")
        self.status_label.setStyleSheet("color: #A0AEC0; font-size: 13px;")
        title_box.addWidget(title)
        title_box.addWidget(self.status_label)
        header_layout.addLayout(title_box)

        header_layout.addStretch()

        self.auth_btn = QPushButton("Connect Account")
        self.auth_btn.setCursor(Qt.PointingHandCursor)
        self.auth_btn.clicked.connect(self._toggle_auth)
        header_layout.addWidget(self.auth_btn)

        self.import_creds_btn = QPushButton("📁 Import OAuth JSON")
        self.import_creds_btn.setCursor(Qt.PointingHandCursor)
        self.import_creds_btn.clicked.connect(self._import_credentials_json)
        header_layout.addWidget(self.import_creds_btn)

        self.setup_help_btn = QPushButton("OAuth Setup Guide")
        self.setup_help_btn.setCursor(Qt.PointingHandCursor)
        self.setup_help_btn.clicked.connect(self._show_oauth_guide)
        header_layout.addWidget(self.setup_help_btn)

        self.refresh_mail_btn = QPushButton("🔄 Check Mail")
        self.refresh_mail_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_mail_btn.clicked.connect(self._load_mailbox)
        header_layout.addWidget(self.refresh_mail_btn)

        layout.addWidget(header_card)

        # Main Splitter: Left = Mailbox & Detail, Right = Compose Draft
        splitter = QSplitter(Qt.Horizontal, self)

        # Left Container: Messages
        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 5, 0)
        left_layout.setSpacing(10)

        search_row = QHBoxLayout()
        self.email_search_input = QLineEdit()
        self.email_search_input.setPlaceholderText("🔍 Search emails (e.g. from:support, is:unread)...")
        self.email_search_input.setFixedHeight(34)
        self.email_search_input.returnPressed.connect(self._load_mailbox)
        search_row.addWidget(self.email_search_input)

        search_btn = QPushButton("Search")
        search_btn.setFixedHeight(34)
        search_btn.clicked.connect(self._load_mailbox)
        search_row.addWidget(search_btn)
        left_layout.addLayout(search_row)

        self.mail_scroll = QScrollArea()
        self.mail_scroll.setWidgetResizable(True)
        self.mail_list_container = QWidget()
        self.mail_list_layout = QVBoxLayout(self.mail_list_container)
        self.mail_list_layout.setContentsMargins(0, 0, 0, 0)
        self.mail_list_layout.setSpacing(6)
        self.mail_list_layout.addStretch()
        self.mail_scroll.setWidget(self.mail_list_container)
        left_layout.addWidget(self.mail_scroll)

        # Pagination controls bar
        page_bar = QHBoxLayout()
        self.prev_page_btn = QPushButton("◀ Previous Page")
        self.prev_page_btn.setFixedHeight(30)
        self.prev_page_btn.setEnabled(False)
        self.prev_page_btn.setCursor(Qt.PointingHandCursor)
        self.prev_page_btn.clicked.connect(self._load_prev_page)

        self.next_page_btn = QPushButton("Next Page ▶")
        self.next_page_btn.setFixedHeight(30)
        self.next_page_btn.setEnabled(False)
        self.next_page_btn.setCursor(Qt.PointingHandCursor)
        self.next_page_btn.clicked.connect(self._load_next_page)

        page_bar.addWidget(self.prev_page_btn)
        page_bar.addWidget(self.next_page_btn)
        left_layout.addLayout(page_bar)

        splitter.addWidget(left_container)

        # Right Container: Compose & Drafts
        right_container = LiquidGlassCard(self, corner_radius=18)
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(18, 14, 18, 14)
        right_layout.setSpacing(10)

        top_draft_row = QHBoxLayout()
        top_draft_row.addWidget(QLabel("<b style='color: #7FFFD4; font-size: 14px;'>Compose / Draft Email</b>"), stretch=1)

        self.new_draft_btn = QPushButton("➕ New")
        self.new_draft_btn.setFixedHeight(28)
        self.new_draft_btn.setCursor(Qt.PointingHandCursor)
        self.new_draft_btn.clicked.connect(self._clear_active_draft)
        top_draft_row.addWidget(self.new_draft_btn)

        self.saved_drafts_btn = QPushButton("📂 Gmail Drafts")
        self.saved_drafts_btn.setFixedHeight(28)
        self.saved_drafts_btn.setCursor(Qt.PointingHandCursor)
        self.saved_drafts_btn.clicked.connect(self._open_saved_drafts_dialog)
        top_draft_row.addWidget(self.saved_drafts_btn)

        right_layout.addLayout(top_draft_row)

        self.draft_status_lbl = QLabel("Status: Local Unsaved Draft")
        self.draft_status_lbl.setStyleSheet("color: #A0AEC0; font-size: 11px; margin-bottom: 4px;")
        right_layout.addWidget(self.draft_status_lbl)

        self.to_input = QLineEdit()
        self.to_input.setPlaceholderText("Recipient (To: email@example.com)...")
        self.to_input.setFixedHeight(34)
        right_layout.addWidget(self.to_input)

        self.subject_input = QLineEdit()
        self.subject_input.setPlaceholderText("Subject...")
        self.subject_input.setFixedHeight(34)
        right_layout.addWidget(self.subject_input)

        self.body_input = QTextEdit()
        self.body_input.setPlaceholderText("Email content...")
        right_layout.addWidget(self.body_input, stretch=1)

        btn_row = QHBoxLayout()
        self.save_draft_btn = QPushButton("💾 Save as Draft")
        self.save_draft_btn.setFixedHeight(36)
        self.save_draft_btn.setCursor(Qt.PointingHandCursor)
        self.save_draft_btn.clicked.connect(self._save_draft)
        btn_row.addWidget(self.save_draft_btn)

        self.send_mail_btn = QPushButton("🚀 Send Email")
        self.send_mail_btn.setFixedHeight(36)
        self.send_mail_btn.setCursor(Qt.PointingHandCursor)
        self.send_mail_btn.setStyleSheet("background-color: rgba(0, 200, 255, 60); font-weight: bold;")
        self.send_mail_btn.clicked.connect(self._send_email_confirmed)
        btn_row.addWidget(self.send_mail_btn)

        right_layout.addLayout(btn_row)
        splitter.addWidget(right_container)

        splitter.setSizes([550, 450])
        layout.addWidget(splitter, stretch=1)

    def update_auth_status(self):
        if self.gmail_service.is_authenticated():
            email = self.gmail_service.get_user_email() or "Connected"
            self.status_label.setText(f"Connected: {email}")
            self.status_label.setStyleSheet("color: #7FFFD4; font-size: 13px;")
            self.auth_btn.setText("Disconnect")
            self.refresh_mail_btn.setEnabled(True)
            self.save_draft_btn.setEnabled(True)
            self.send_mail_btn.setEnabled(True)
        else:
            has_creds = self.gmail_service.has_client_credentials()
            if has_creds:
                self.status_label.setText("Not connected (credentials JSON found, ready for login)")
            else:
                self.status_label.setText("Not connected (place gmail_credentials.json in data/ folder)")
            self.status_label.setStyleSheet("color: #FFAA00; font-size: 13px;")
            self.auth_btn.setText("Connect Account")
            self.refresh_mail_btn.setEnabled(False)
            self.save_draft_btn.setEnabled(False)
            self.send_mail_btn.setEnabled(False)

    def _toggle_auth(self):
        if self.gmail_service.is_authenticated():
            self.gmail_service.disconnect()
            self.update_auth_status()
            show_info_message("Disconnected", "Gmail account disconnected.")
        else:
            if not self.gmail_service.has_client_credentials():
                show_error_message(
                    "Credentials Required",
                    f"Please place your OAuth client secrets file at:\n{self.gmail_service.get_credentials_path()}\n\n"
                    "Click 'OAuth Setup Guide' for step-by-step instructions."
                )
                return
            try:
                self.gmail_service.authenticate_oauth()
                self.update_auth_status()
                self._load_mailbox()
            except Exception as e:
                show_error_message("OAuth Authentication Failed", str(e))

    def _import_credentials_json(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Google OAuth Client Secrets JSON", "", "JSON Files (*.json)"
        )
        if file_path:
            try:
                dest = self.gmail_service.get_credentials_path()
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(file_path, dest)
                show_info_message("Credentials Imported", f"OAuth credentials imported successfully to:\n{dest}")
                self.update_auth_status()
            except Exception as e:
                show_error_message("Import Error", f"Failed to import credentials: {e}")

    def _show_oauth_guide(self):
        guide = (
            "<h3>Google Gmail OAuth Setup Guide</h3>"
            "<ol>"
            "<li>Go to <a href='https://console.cloud.google.com/'>Google Cloud Console</a>.</li>"
            "<li>Create a project and enable the <b>Gmail API</b>.</li>"
            "<li>Configure the OAuth consent screen (Desktop App).</li>"
            "<li>Create credentials: <b>OAuth client ID</b> > <b>Desktop app</b>.</li>"
            "<li>Download the JSON file and click <b>Import OAuth JSON</b> above (or place it manually at <code>data/gmail_credentials.json</code>).</li>"
            "<li>Click <b>Connect Account</b> to complete browser authorization.</li>"
            "</ol>"
        )
        dlg = QMessageBox(self)
        dlg.setWindowTitle("OAuth Setup Guide")
        dlg.setTextFormat(Qt.RichText)
        dlg.setText(guide)
        dlg.exec()

    @asyncSlot()
    async def _load_mailbox(self, page_token: Optional[str] = None):
        if not self.gmail_service.is_authenticated():
            return

        while self.mail_list_layout.count() > 1:
            child = self.mail_list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        loading = QLabel("<p style='color: #00C8FF; text-align: center; margin: 20px;'>Loading emails from Gmail...</p>")
        self.mail_list_layout.insertWidget(0, loading)

        q = self.email_search_input.text().strip() or None
        try:
            summaries, next_token = await asyncio.to_thread(
                self.gmail_service.list_messages, query=q, page_token=page_token
            )
            loading.deleteLater()

            self._next_page_token = next_token
            self.next_page_btn.setEnabled(bool(next_token))
            self.prev_page_btn.setEnabled(len(self._page_tokens_history) > 0)

            if not summaries:
                empty = QLabel("<p style='color: #718096; text-align: center; margin: 30px;'>No messages found.</p>")
                empty.setAlignment(Qt.AlignCenter)
                self.mail_list_layout.insertWidget(0, empty)
                return

            for summary in summaries:
                card = self._create_email_summary_widget(summary)
                self.mail_list_layout.insertWidget(self.mail_list_layout.count() - 1, card)
        except Exception as e:
            loading.deleteLater()
            show_error_message("Mailbox Error", f"Failed to retrieve emails: {e}")

    @asyncSlot()
    async def _load_next_page(self):
        if self._next_page_token:
            self._page_tokens_history.append(self._current_page_token)
            self._current_page_token = self._next_page_token
            await self._load_mailbox(page_token=self._current_page_token)

    @asyncSlot()
    async def _load_prev_page(self):
        if self._page_tokens_history:
            prev_token = self._page_tokens_history.pop()
            self._current_page_token = prev_token
            await self._load_mailbox(page_token=self._current_page_token)

    def _create_email_summary_widget(self, s: EmailSummary) -> QWidget:
        card = LiquidGlassCard(self.mail_list_container, corner_radius=12)
        c_layout = QVBoxLayout(card)
        c_layout.setContentsMargins(12, 10, 12, 10)
        c_layout.setSpacing(4)

        top = QHBoxLayout()
        sender_lbl = QLabel(f"<b style='color: #00C8FF;'>{s.sender}</b>")
        date_lbl = QLabel(s.date[:16] if len(s.date) > 16 else s.date)
        date_lbl.setStyleSheet("color: #718096; font-size: 11px;")
        top.addWidget(sender_lbl, stretch=1)
        top.addWidget(date_lbl)
        c_layout.addLayout(top)

        subj_lbl = QLabel(f"<b style='color: #F0F4F8;'>{s.subject}</b>")
        c_layout.addWidget(subj_lbl)

        snippet_lbl = QLabel(s.snippet)
        snippet_lbl.setStyleSheet("color: #A0AEC0; font-size: 11px;")
        snippet_lbl.setWordWrap(True)
        c_layout.addWidget(snippet_lbl)

        view_btn = QPushButton("View Full Message")
        view_btn.setFixedHeight(26)
        view_btn.setCursor(Qt.PointingHandCursor)
        view_btn.clicked.connect(lambda _, mid=s.id: self._open_message_detail(mid))
        c_layout.addWidget(view_btn, alignment=Qt.AlignRight)

        return card

    @asyncSlot()
    async def _open_message_detail(self, message_id: str):
        try:
            detail: EmailDetail = await asyncio.to_thread(
                self.gmail_service.get_message_detail, message_id
            )
            if not detail:
                return

            dlg = QDialog(self)
            dlg.setWindowTitle(detail.subject)
            dlg.resize(650, 500)
            d_layout = QVBoxLayout(dlg)

            header_box = QVBoxLayout()
            header_box.addWidget(QLabel(f"<b>From:</b> {detail.sender}"))
            header_box.addWidget(QLabel(f"<b>To:</b> {detail.recipient}"))
            header_box.addWidget(QLabel(f"<b>Date:</b> {detail.date}"))
            header_box.addWidget(QLabel(f"<b>Subject:</b> {detail.subject}"))
            d_layout.addLayout(header_box)

            body_view = QTextEdit()
            body_view.setReadOnly(True)
            if detail.body_html:
                body_view.setHtml(detail.body_html)
            else:
                body_view.setPlainText(detail.body_text)
            d_layout.addWidget(body_view)

            # AI Summary Display Box
            summary_display = QLabel()
            summary_display.setStyleSheet("background: rgba(0, 200, 255, 20); border: 1px solid rgba(0, 200, 255, 60); padding: 10px; border-radius: 8px; color: #E2E8F0; font-size: 12px;")
            summary_display.setWordWrap(True)
            summary_display.hide()
            d_layout.addWidget(summary_display)

            # AI Action Buttons Row
            ai_btn_row = QHBoxLayout()

            summarize_btn = QPushButton("✨ AI Summarize & Classify")
            summarize_btn.setFixedHeight(34)
            summarize_btn.setCursor(Qt.PointingHandCursor)

            reply_draft_btn = QPushButton("✍️ AI Generate Reply Draft")
            reply_draft_btn.setFixedHeight(34)
            reply_draft_btn.setCursor(Qt.PointingHandCursor)

            ai_btn_row.addWidget(summarize_btn)
            ai_btn_row.addWidget(reply_draft_btn)
            d_layout.addLayout(ai_btn_row)

            close_btn = QPushButton("Close")
            close_btn.clicked.connect(dlg.accept)
            d_layout.addWidget(close_btn)

            async def _on_summarize():
                summarize_btn.setEnabled(False)
                summary_display.setText("AI is analyzing and summarizing email...")
                summary_display.show()
                try:
                    ai_prov = getattr(self.window(), "ai_provider", None)
                    res = await asyncio.to_thread(summarize_and_classify_email, detail, ai_prov)
                    urgency_colors = {
                        "critical": "#FF4444",
                        "high": "#FFAA00",
                        "medium": "#00C8FF",
                        "low": "#7FFFD4",
                    }
                    u_col = urgency_colors.get(res.get("urgency", "low"), "#7FFFD4")
                    text = (
                        f"<b>AI Urgency Rating:</b> <span style='color:{u_col}; font-weight:bold;'>{res['urgency'].upper()}</span><br>"
                        f"<b>AI Summary:</b> {res['summary']}"
                    )
                    summary_display.setText(text)
                except Exception as ex:
                    summary_display.setText(f"AI Summarization Error: {ex}")
                finally:
                    summarize_btn.setEnabled(True)

            async def _on_generate_reply():
                reply_draft_btn.setEnabled(False)
                try:
                    ai_prov = getattr(self.window(), "ai_provider", None)
                    res = await asyncio.to_thread(generate_reply_draft, detail, ai_prov)
                    self._active_draft_id = None
                    self.to_input.setText(detail.sender)
                    self.subject_input.setText(res.get("subject", f"Re: {detail.subject}"))
                    self.body_input.setText(res.get("body", ""))
                    self._update_draft_status_ui()
                    dlg.accept()
                    show_info_message("Reply Draft Generated", "Proposed reply draft populated in Compose tab for your review.")
                except Exception as ex:
                    show_error_message("Draft Error", str(ex))
                finally:
                    reply_draft_btn.setEnabled(True)

            summarize_btn.clicked.connect(lambda *_: asyncio.create_task(_on_summarize()))
            reply_draft_btn.clicked.connect(lambda *_: asyncio.create_task(_on_generate_reply()))

            dlg.exec()
        except Exception as e:
            show_error_message("Read Error", str(e))

    def _clear_active_draft(self):
        self._active_draft_id = None
        self.to_input.clear()
        self.subject_input.clear()
        self.body_input.clear()
        self._update_draft_status_ui()

    def _update_draft_status_ui(self):
        if self._active_draft_id:
            self.draft_status_lbl.setText(f"Status: Saved in Gmail (ID: {self._active_draft_id[:8]}...)")
            self.draft_status_lbl.setStyleSheet("color: #7FFFD4; font-size: 11px; font-weight: bold;")
        else:
            self.draft_status_lbl.setText("Status: Local Unsaved Draft")
            self.draft_status_lbl.setStyleSheet("color: #FFAA00; font-size: 11px;")

    @asyncSlot()
    async def _open_saved_drafts_dialog(self):
        if not self.gmail_service.is_authenticated():
            show_error_message("Authentication Required", "Please connect your Gmail account to view saved drafts.")
            return

        try:
            drafts = await asyncio.to_thread(self.gmail_service.list_drafts)
            if not drafts:
                show_info_message("No Drafts", "No saved Gmail drafts found.")
                return

            dlg = QDialog(self)
            dlg.setWindowTitle("Saved Gmail Drafts")
            dlg.resize(500, 350)
            d_layout = QVBoxLayout(dlg)

            d_layout.addWidget(QLabel("<b>Select a saved Gmail draft to edit:</b>"))

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            content = QWidget()
            c_layout = QVBoxLayout(content)
            c_layout.setSpacing(6)

            for draft in drafts:
                btn = QPushButton(f"Subject: {draft.subject or '(No Subject)'} | To: {draft.recipient or '(No Recipient)'}")
                btn.setCursor(Qt.PointingHandCursor)
                btn.clicked.connect(lambda _, did=draft.id: self._load_draft_to_compose(did, dlg))
                c_layout.addWidget(btn)

            c_layout.addStretch()
            scroll.setWidget(content)
            d_layout.addWidget(scroll)

            close_btn = QPushButton("Cancel")
            close_btn.clicked.connect(dlg.reject)
            d_layout.addWidget(close_btn)

            dlg.exec()
        except Exception as e:
            show_error_message("Drafts Error", str(e))

    @asyncSlot()
    async def _load_draft_to_compose(self, draft_id: str, dialog: QDialog):
        try:
            draft = await asyncio.to_thread(self.gmail_service.get_draft_detail, draft_id)
            if draft:
                self._active_draft_id = draft.id
                self.to_input.setText(draft.recipient)
                self.subject_input.setText(draft.subject)
                self.body_input.setText(draft.body)
                self._update_draft_status_ui()
                dialog.accept()
        except Exception as e:
            show_error_message("Load Draft Error", str(e))

    @asyncSlot()
    async def _save_draft(self):
        to = self.to_input.text().strip()
        subj = self.subject_input.text().strip()
        body = self.body_input.toPlainText()

        try:
            if self._active_draft_id:
                draft = await asyncio.to_thread(
                    self.gmail_service.update_draft, self._active_draft_id, to, subj, body
                )
                show_info_message("Draft Updated", f"Gmail draft updated successfully (ID: {draft.id})")
            else:
                draft = await asyncio.to_thread(self.gmail_service.create_draft, to, subj, body)
                self._active_draft_id = draft.id
                show_info_message("Draft Saved", f"Draft saved successfully in Gmail (ID: {draft.id})")

            self._update_draft_status_ui()
        except Exception as e:
            show_error_message("Draft Error", str(e))

    @asyncSlot()
    async def _send_email_confirmed(self):
        to = self.to_input.text().strip()
        subj = self.subject_input.text().strip()
        body = self.body_input.toPlainText()

        if not to:
            show_error_message("Validation Error", "Recipient 'To:' address cannot be empty.")
            return

        # Explicit user confirmation dialog
        reply = QMessageBox.question(
            self,
            "Confirm Send Email",
            f"Are you sure you want to send this email?\n\nTo: {to}\nSubject: {subj}\n\nThis action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            sent_id = await asyncio.to_thread(
                self.gmail_service.send_email, to, subj, body, user_confirmed=True
            )
            show_info_message("Email Sent", f"Email successfully dispatched! (ID: {sent_id})")
            self.to_input.clear()
            self.subject_input.clear()
            self.body_input.clear()
        except Exception as e:
            show_error_message("Send Error", str(e))


# ----------------------------------------------------------------------
# 4. NOTIFICATION INTELLIGENCE & ALERT CENTER WIDGET
# ----------------------------------------------------------------------

class NotificationCenterWidget(QWidget):
    """
    Centralized Alert Center View.
    Aggregates, ranks, sorts, and displays WhatsApp & Gmail notifications.
    Supports source/category/urgency filtering, search, mark read, and dismissal controls.
    """

    def __init__(self, alert_service: AlertNotificationService, parent=None):
        super().__init__(parent)
        self.alert_service = alert_service
        self._setup_ui()
        self.refresh_alerts()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        # Header Card
        header_card = LiquidGlassCard(self, corner_radius=18)
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(18, 12, 18, 12)

        title_box = QVBoxLayout()
        title = QLabel("<h2 style='color: #00C8FF; margin: 0;'>Notification Intelligence & Alert Center</h2>")
        self.status_lbl = QLabel("Aggregated intelligence feed across WhatsApp Desktop and Gmail")
        self.status_lbl.setStyleSheet("color: #A0AEC0; font-size: 13px;")
        title_box.addWidget(title)
        title_box.addWidget(self.status_lbl)
        header_layout.addLayout(title_box)

        header_layout.addStretch()

        self.critical_badge = QLabel(" 0 CRITICAL ")
        self.critical_badge.setStyleSheet("background: rgba(255, 68, 68, 40); color: #FF4444; border: 1px solid #FF4444; border-radius: 6px; font-weight: bold; padding: 4px 8px;")
        header_layout.addWidget(self.critical_badge)

        self.unread_badge = QLabel(" 0 UNREAD ")
        self.unread_badge.setStyleSheet("background: rgba(0, 200, 255, 40); color: #00C8FF; border: 1px solid #00C8FF; border-radius: 6px; font-weight: bold; padding: 4px 8px;")
        header_layout.addWidget(self.unread_badge)

        refresh_btn = QPushButton("🔄 Refresh Feed")
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.clicked.connect(self.refresh_alerts)
        header_layout.addWidget(refresh_btn)

        layout.addWidget(header_card)

        # Filter & Action Bar
        filter_card = LiquidGlassCard(self, corner_radius=16)
        f_layout = QHBoxLayout(filter_card)
        f_layout.setContentsMargins(14, 10, 14, 10)
        f_layout.setSpacing(8)

        self.search_in = QLineEdit()
        self.search_in.setPlaceholderText("🔍 Search alerts...")
        self.search_in.setFixedHeight(32)
        self.search_in.textChanged.connect(self.refresh_alerts)
        f_layout.addWidget(self.search_in, stretch=2)

        self.source_combo = QComboBox()
        self.source_combo.addItems(["All Sources", "WhatsApp", "Gmail"])
        self.source_combo.setFixedHeight(32)
        self.source_combo.currentIndexChanged.connect(self.refresh_alerts)
        f_layout.addWidget(self.source_combo, stretch=1)

        self.cat_combo = QComboBox()
        self.cat_combo.addItems(["All Categories", "Work", "Family", "VIP", "Email", "General"])
        self.cat_combo.setFixedHeight(32)
        self.cat_combo.currentIndexChanged.connect(self.refresh_alerts)
        f_layout.addWidget(self.cat_combo, stretch=1)

        self.urgency_combo = QComboBox()
        self.urgency_combo.addItems(["All Urgencies", "CRITICAL", "HIGH", "NORMAL", "LOW"])
        self.urgency_combo.setFixedHeight(32)
        self.urgency_combo.currentIndexChanged.connect(self.refresh_alerts)
        f_layout.addWidget(self.urgency_combo, stretch=1)

        self.unread_chk = QCheckBox("Unread Only")
        self.unread_chk.toggled.connect(self.refresh_alerts)
        f_layout.addWidget(self.unread_chk)

        mark_all_btn = QPushButton("✓ Mark All Read")
        mark_all_btn.setFixedHeight(32)
        mark_all_btn.setCursor(Qt.PointingHandCursor)
        mark_all_btn.clicked.connect(self._mark_all_read_action)
        f_layout.addWidget(mark_all_btn)

        clear_btn = QPushButton("🗑️ Clear")
        clear_btn.setFixedHeight(32)
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet("color: #FF6B6B;")
        clear_btn.clicked.connect(self._clear_history_action)
        f_layout.addWidget(clear_btn)

        layout.addWidget(filter_card)

        # Scrollable Feed Container
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.alert_list_container = QWidget()
        self.alert_list_layout = QVBoxLayout(self.alert_list_container)
        self.alert_list_layout.setContentsMargins(0, 0, 0, 0)
        self.alert_list_layout.setSpacing(8)
        self.alert_list_layout.addStretch()
        self.scroll_area.setWidget(self.alert_list_container)

        layout.addWidget(self.scroll_area, stretch=1)

    def refresh_alerts(self):
        while self.alert_list_layout.count() > 1:
            child = self.alert_list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        src = self.source_combo.currentText()
        source_filter = None if src == "All Sources" else src

        cat = self.cat_combo.currentText()
        cat_filter = None if cat == "All Categories" else cat

        urg = self.urgency_combo.currentText()
        urgency_filter = None if urg == "All Urgencies" else urg.lower()

        search = self.search_in.text().strip() or None
        unread = self.unread_chk.isChecked()

        alerts = self.alert_service.get_alerts(
            source_filter=source_filter,
            category_filter=cat_filter,
            urgency_filter=urgency_filter,
            unread_only=unread,
            search_query=search,
        )

        unread_cnt = self.alert_service.get_unread_count()
        critical_cnt = self.alert_service.get_critical_count()
        self.unread_badge.setText(f" {unread_cnt} UNREAD ")
        self.critical_badge.setText(f" {critical_cnt} CRITICAL/HIGH ")

        if not alerts:
            empty = QLabel("<p style='color: #718096; text-align: center; margin: 40px;'>No alert notifications matching criteria.</p>")
            empty.setAlignment(Qt.AlignCenter)
            self.alert_list_layout.insertWidget(0, empty)
            return

        for alert in alerts:
            card = self._create_alert_card(alert)
            self.alert_list_layout.insertWidget(self.alert_list_layout.count() - 1, card)

    def _create_alert_card(self, alert: AlertNotification) -> QWidget:
        is_high = alert.urgency in ("critical", "high")
        card = LiquidGlassCard(self.alert_list_container, corner_radius=14)

        if is_high and not alert.is_read:
            card.setStyleSheet("background-color: rgba(255, 68, 68, 25); border: 1px solid rgba(255, 68, 68, 120);")

        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        top = QHBoxLayout()
        icon = "📱" if alert.source == "WhatsApp" else "📧"
        src_lbl = QLabel(f"<b>{icon} {alert.source}</b>")
        src_lbl.setStyleSheet("color: #00C8FF; font-size: 12px;")
        top.addWidget(src_lbl)

        cat_lbl = QLabel(f"[{alert.category}]")
        cat_lbl.setStyleSheet("color: #A0AEC0; font-size: 11px;")
        top.addWidget(cat_lbl)

        sender_lbl = QLabel(f"<b>{alert.sender}</b>")
        sender_lbl.setStyleSheet("color: #F0F4F8; font-size: 13px;")
        top.addWidget(sender_lbl, stretch=1)

        time_lbl = QLabel(alert.timestamp[:19] if len(alert.timestamp) > 19 else alert.timestamp)
        time_lbl.setStyleSheet("color: #718096; font-size: 11px;")
        top.addWidget(time_lbl)

        u_cols = {"critical": "#FF4444", "high": "#FFAA00", "normal": "#00C8FF", "low": "#7FFFD4"}
        u_col = u_cols.get(alert.urgency.lower(), "#7FFFD4")
        u_badge = QLabel(f" {alert.urgency.upper()} ")
        u_badge.setStyleSheet(f"color: {u_col}; border: 1px solid {u_col}; border-radius: 4px; font-size: 10px; font-weight: bold;")
        top.addWidget(u_badge)

        layout.addLayout(top)

        title_style = "font-weight: bold; color: #E2E8F0;" if not alert.is_read else "color: #A0AEC0;"
        title_lbl = QLabel(f"<span style='{title_style} font-size: 13px;'>{alert.title}</span>")
        layout.addWidget(title_lbl)

        body_lbl = QLabel(alert.body_preview)
        body_lbl.setStyleSheet("color: #CBD5E0; font-size: 12px;")
        body_lbl.setWordWrap(True)
        layout.addWidget(body_lbl)

        if alert.summary:
            sum_lbl = QLabel(f"<i style='color: #7FFFD4; font-size: 11px;'>AI Summary: {alert.summary}</i>")
            sum_lbl.setWordWrap(True)
            layout.addWidget(sum_lbl)

        bot_row = QHBoxLayout()
        bot_row.addStretch()

        if not alert.is_read:
            read_btn = QPushButton("✓ Mark Read")
            read_btn.setFixedHeight(26)
            read_btn.setCursor(Qt.PointingHandCursor)
            read_btn.clicked.connect(lambda _, aid=alert.id: self._mark_read_action(aid))
            bot_row.addWidget(read_btn)

        dismiss_btn = QPushButton("Dismiss")
        dismiss_btn.setFixedHeight(26)
        dismiss_btn.setCursor(Qt.PointingHandCursor)
        dismiss_btn.clicked.connect(lambda _, aid=alert.id: self._dismiss_action(aid))
        bot_row.addWidget(dismiss_btn)

        layout.addLayout(bot_row)
        return card

    def _mark_read_action(self, alert_id: str):
        self.alert_service.mark_as_read(alert_id)
        self.refresh_alerts()

    def _dismiss_action(self, alert_id: str):
        self.alert_service.dismiss_alert(alert_id)
        self.refresh_alerts()

    def _mark_all_read_action(self):
        count = self.alert_service.mark_all_as_read()
        self.refresh_alerts()
        show_info_message("Alert Center", f"Marked {count} notifications as read.")

    def _clear_history_action(self):
        reply = QMessageBox.question(
            self,
            "Clear Alert Center",
            "Are you sure you want to clear all notification history?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.alert_service.clear_history()
            self.refresh_alerts()


# ----------------------------------------------------------------------
# 5. WHATSAPP PERSONAL COMPANION WIDGET
# ----------------------------------------------------------------------

class WhatsAppCompanionWidget(QWidget):
    """
    Dedicated view for WhatsApp Personal Companion Mode:
    Windows WhatsApp Desktop notification listener monitoring,
    important contacts priorities, manual message import/paste fallback,
    AI summarization, urgency classification, AI reply drafting, and clipboard copying.
    """

    def __init__(self, whatsapp_service: WhatsAppService, notification_listener: Optional[WhatsAppNotificationListener] = None, parent=None):
        super().__init__(parent)
        self.whatsapp_service = whatsapp_service
        self.notification_listener = notification_listener
        self._setup_ui()
        self.refresh_contacts()
        self.refresh_history()

        if self.notification_listener:
            self.notification_listener.notification_received.connect(lambda *_: self.refresh_history())
            self.notification_listener.status_changed.connect(self._on_listener_status_changed)
            # Initial listener check
            self._check_listener_status()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        # Header Card
        header_card = LiquidGlassCard(self, corner_radius=18)
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(18, 12, 18, 12)

        title_box = QVBoxLayout()
        title = QLabel("<h2 style='color: #25D366; margin: 0;'>WhatsApp Personal Companion</h2>")
        desc = QLabel("AI assistant for personal WhatsApp Desktop workflows (Manual Import & Clipboard Assistant)")
        desc.setStyleSheet("color: #A0AEC0; font-size: 13px;")
        title_box.addWidget(title)
        title_box.addWidget(desc)
        header_layout.addLayout(title_box)

        header_layout.addStretch()

        self.toggle_chk = QCheckBox("Enable Companion Mode")
        self.toggle_chk.setChecked(self.whatsapp_service.is_enabled())
        self.toggle_chk.toggled.connect(self._on_toggle_companion)
        header_layout.addWidget(self.toggle_chk)

        layout.addWidget(header_card)

        # Live Notification Monitoring Status Card
        monitor_card = LiquidGlassCard(self, corner_radius=16)
        m_layout = QHBoxLayout(monitor_card)
        m_layout.setContentsMargins(16, 10, 16, 10)

        self.monitor_status_lbl = QLabel("Live Monitoring: Manual Import / Clipboard Mode")
        self.monitor_status_lbl.setStyleSheet("color: #FFAA00; font-size: 12px; font-weight: bold;")
        m_layout.addWidget(self.monitor_status_lbl, stretch=1)

        self.toggle_monitor_btn = QPushButton("▶ Start Live Monitoring")
        self.toggle_monitor_btn.setFixedHeight(32)
        self.toggle_monitor_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_monitor_btn.clicked.connect(self._toggle_live_monitoring)
        m_layout.addWidget(self.toggle_monitor_btn)

        layout.addWidget(monitor_card)

        # Main Splitter: Left = Contacts & Paste Import, Right = History & AI Reply
        splitter = QSplitter(Qt.Horizontal, self)

        # Left Container
        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 5, 0)
        left_layout.setSpacing(10)

        # 1. Smart Contact Intelligence Card
        contacts_card = LiquidGlassCard(left_container, corner_radius=16)
        c_layout = QVBoxLayout(contacts_card)
        c_layout.setContentsMargins(14, 12, 14, 12)
        c_layout.setSpacing(8)

        c_top = QHBoxLayout()
        c_top.addWidget(QLabel("<b style='color: #25D366;'>Smart Contact Intelligence & Priorities</b>"), stretch=1)
        reset_contacts_btn = QPushButton("🔄 Reset Defaults")
        reset_contacts_btn.setFixedHeight(26)
        reset_contacts_btn.setCursor(Qt.PointingHandCursor)
        reset_contacts_btn.clicked.connect(self._reset_contacts_action)
        c_top.addWidget(reset_contacts_btn)
        c_layout.addLayout(c_top)

        add_row1 = QHBoxLayout()
        self.contact_name_in = QLineEdit()
        self.contact_name_in.setPlaceholderText("Contact name or phone...")
        self.contact_name_in.setFixedHeight(32)
        add_row1.addWidget(self.contact_name_in, stretch=2)

        self.contact_pri_combo = QComboBox()
        self.contact_pri_combo.addItems(["HIGH", "CRITICAL", "NORMAL", "LOW"])
        self.contact_pri_combo.setFixedHeight(32)
        add_row1.addWidget(self.contact_pri_combo, stretch=1)

        self.contact_cat_combo = QComboBox()
        self.contact_cat_combo.addItems(["Work", "Family", "VIP", "General"])
        self.contact_cat_combo.setFixedHeight(32)
        add_row1.addWidget(self.contact_cat_combo, stretch=1)
        c_layout.addLayout(add_row1)

        add_row2 = QHBoxLayout()
        self.contact_trusted_chk = QCheckBox("⭐ Trusted Contact")
        add_row2.addWidget(self.contact_trusted_chk)

        self.contact_notes_in = QLineEdit()
        self.contact_notes_in.setPlaceholderText("Optional notes...")
        self.contact_notes_in.setFixedHeight(30)
        add_row2.addWidget(self.contact_notes_in, stretch=1)

        add_contact_btn = QPushButton("➕ Add Contact")
        add_contact_btn.setFixedHeight(32)
        add_contact_btn.setCursor(Qt.PointingHandCursor)
        add_contact_btn.clicked.connect(self._add_contact)
        add_row2.addWidget(add_contact_btn)
        c_layout.addLayout(add_row2)

        self.contacts_scroll = QScrollArea()
        self.contacts_scroll.setWidgetResizable(True)
        self.contacts_container = QWidget()
        self.contacts_list_layout = QVBoxLayout(self.contacts_container)
        self.contacts_list_layout.setContentsMargins(0, 0, 0, 0)
        self.contacts_list_layout.setSpacing(4)
        self.contacts_scroll.setWidget(self.contacts_container)
        self.contacts_scroll.setFixedHeight(140)
        c_layout.addWidget(self.contacts_scroll)

        left_layout.addWidget(contacts_card)

        # 2. Paste / Import Message Card
        import_card = LiquidGlassCard(left_container, corner_radius=16)
        i_layout = QVBoxLayout(import_card)
        i_layout.setContentsMargins(14, 12, 14, 12)
        i_layout.setSpacing(8)
        i_layout.addWidget(QLabel("<b style='color: #7FFFD4;'>Import Message / Paste Notification</b>"))

        self.sender_in = QLineEdit()
        self.sender_in.setPlaceholderText("Sender name or phone number...")
        self.sender_in.setFixedHeight(34)
        i_layout.addWidget(self.sender_in)

        self.msg_text_in = QTextEdit()
        self.msg_text_in.setPlaceholderText("Paste WhatsApp message content here...")
        self.msg_text_in.setFixedHeight(110)
        i_layout.addWidget(self.msg_text_in)

        analyze_btn = QPushButton("⚡ Analyze & Summarize Message")
        analyze_btn.setFixedHeight(36)
        analyze_btn.setCursor(Qt.PointingHandCursor)
        analyze_btn.setStyleSheet("background-color: rgba(37, 211, 102, 60); font-weight: bold;")
        analyze_btn.clicked.connect(self._analyze_imported_message)
        i_layout.addWidget(analyze_btn)

        left_layout.addWidget(import_card, stretch=1)
        splitter.addWidget(left_container)

        # Right Container: Analyzed History & AI Reply Generation
        right_container = LiquidGlassCard(self, corner_radius=18)
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(16, 12, 16, 12)
        right_layout.setSpacing(8)

        right_top = QHBoxLayout()
        right_top.addWidget(QLabel("<b style='color: #00C8FF; font-size: 14px;'>Analyzed Message History & AI Replies</b>"), stretch=1)

        clear_hist_btn = QPushButton("🗑️ Clear History")
        clear_hist_btn.setFixedHeight(28)
        clear_hist_btn.setCursor(Qt.PointingHandCursor)
        clear_hist_btn.setStyleSheet("color: #FF6B6B;")
        clear_hist_btn.clicked.connect(self._clear_history)
        right_top.addWidget(clear_hist_btn)
        right_layout.addLayout(right_top)

        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_container = QWidget()
        self.history_list_layout = QVBoxLayout(self.history_container)
        self.history_list_layout.setContentsMargins(0, 0, 0, 0)
        self.history_list_layout.setSpacing(8)
        self.history_list_layout.addStretch()
        self.history_scroll.setWidget(self.history_container)
        right_layout.addWidget(self.history_scroll, stretch=1)

        splitter.addWidget(right_container)
        splitter.setSizes([450, 550])
        layout.addWidget(splitter, stretch=1)

    def _on_toggle_companion(self, checked: bool):
        self.whatsapp_service.set_enabled(checked)
        show_info_message("WhatsApp Companion", f"WhatsApp Personal Companion Mode is now {'enabled' if checked else 'disabled'}.")

    def _check_listener_status(self):
        if not self.notification_listener:
            return
        supported, status_msg = self.notification_listener.check_notification_support()
        self._on_listener_status_changed(status_msg, self.notification_listener.is_monitoring)

    def _on_listener_status_changed(self, status_msg: str, is_monitoring: bool):
        self.monitor_status_lbl.setText(f"Live Monitoring: {status_msg}")
        if is_monitoring:
            self.monitor_status_lbl.setStyleSheet("color: #7FFFD4; font-size: 12px; font-weight: bold;")
            self.toggle_monitor_btn.setText("⏹ Stop Live Monitoring")
            self.toggle_monitor_btn.setStyleSheet("background-color: rgba(255, 68, 68, 80); border: 1px solid #FF4444;")
        else:
            self.monitor_status_lbl.setStyleSheet("color: #FFAA00; font-size: 12px; font-weight: bold;")
            self.toggle_monitor_btn.setText("▶ Start Live Monitoring")
            self.toggle_monitor_btn.setStyleSheet("")

    def _toggle_live_monitoring(self):
        if not self.notification_listener:
            show_info_message("Live Monitoring", "Notification listener not initialized.")
            return

        if self.notification_listener.is_monitoring:
            self.notification_listener.stop_monitoring()
            show_info_message("Live Monitoring", "Live Windows notification monitoring stopped.")
        else:
            supported, status_msg = self.notification_listener.start_monitoring()
            if supported:
                show_info_message("Live Monitoring Active", "Live Windows WhatsApp Desktop notification monitoring started.")
            else:
                show_info_message("Manual Import Fallback", f"Could not start live notification capture: {status_msg}")

    def _add_contact(self):
        name = self.contact_name_in.text().strip()
        pri = self.contact_pri_combo.currentText()
        cat = self.contact_cat_combo.currentText()
        trusted = self.contact_trusted_chk.isChecked()
        notes = self.contact_notes_in.text().strip()

        if not name:
            show_error_message("Validation Error", "Contact name cannot be blank.")
            return

        try:
            self.whatsapp_service.add_contact(
                name=name,
                priority=pri,
                category=cat,
                trusted=trusted,
                custom_notes=notes,
            )
            self.contact_name_in.clear()
            self.contact_notes_in.clear()
            self.contact_trusted_chk.setChecked(False)
            self.refresh_contacts()
        except Exception as e:
            show_error_message("Error", str(e))

    def _reset_contacts_action(self):
        reply = QMessageBox.question(
            self,
            "Reset Contacts",
            "Reset all WhatsApp contacts to default preferences?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.whatsapp_service.reset_default_contacts()
            self.refresh_contacts()

    def refresh_all(self):
        """Refreshes both contacts and message history lists."""
        self.refresh_contacts()
        self.refresh_history()

    def refresh_contacts(self):
        while self.contacts_list_layout.count() > 0:
            child = self.contacts_list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        contacts = self.whatsapp_service.get_contacts()
        if not contacts:
            empty = QLabel("<span style='color: #718096; font-size: 11px;'>No contacts configured.</span>")
            self.contacts_list_layout.addWidget(empty)
            return

        for c in contacts:
            row = QWidget()
            r_layout = QHBoxLayout(row)
            r_layout.setContentsMargins(4, 2, 4, 2)
            r_layout.setSpacing(6)

            star = "⭐ " if c.trusted else ""
            lbl = QLabel(f"<b>{star}{c.name}</b> <span style='color: #A0AEC0; font-size: 10px;'>[{c.category}]</span>")
            lbl.setStyleSheet("color: #F0F4F8; font-size: 12px;")
            r_layout.addWidget(lbl, stretch=1)

            pri_colors = {"CRITICAL": "#FF4444", "HIGH": "#FFAA00", "NORMAL": "#00C8FF", "LOW": "#888888"}
            p_col = pri_colors.get(c.priority, "#00C8FF")
            badge = QLabel(f" {c.priority} ")
            badge.setStyleSheet(f"color: {p_col}; border: 1px solid {p_col}; border-radius: 4px; font-size: 10px; font-weight: bold;")
            r_layout.addWidget(badge)

            edit_btn = QPushButton("✏️")
            edit_btn.setFixedSize(24, 24)
            edit_btn.setCursor(Qt.PointingHandCursor)
            edit_btn.setToolTip("Edit Contact")
            edit_btn.clicked.connect(lambda _, contact=c: self._edit_contact_dialog(contact))
            r_layout.addWidget(edit_btn)

            del_btn = QPushButton("❌")
            del_btn.setFixedSize(24, 24)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setToolTip("Remove Contact")
            del_btn.clicked.connect(lambda _, cname=c.name: self._remove_contact(cname))
            r_layout.addWidget(del_btn)

            self.contacts_list_layout.addWidget(row)

    def _edit_contact_dialog(self, contact: ImportantContact):
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit Contact: {contact.name}")
        dlg.setFixedWidth(360)
        d_layout = QVBoxLayout(dlg)
        d_layout.setSpacing(10)

        d_layout.addWidget(QLabel(f"<b>Contact:</b> {contact.name}"))

        d_layout.addWidget(QLabel("Priority Level:"))
        pri_combo = QComboBox()
        pri_combo.addItems(["CRITICAL", "HIGH", "NORMAL", "LOW"])
        pri_combo.setCurrentText(contact.priority)
        d_layout.addWidget(pri_combo)

        d_layout.addWidget(QLabel("Category:"))
        cat_combo = QComboBox()
        cat_combo.addItems(["Work", "Family", "VIP", "General"])
        cat_combo.setCurrentText(contact.category)
        d_layout.addWidget(cat_combo)

        trusted_chk = QCheckBox("⭐ Trusted Contact")
        trusted_chk.setChecked(contact.trusted)
        d_layout.addWidget(trusted_chk)

        d_layout.addWidget(QLabel("Custom Notes:"))
        notes_in = QLineEdit(contact.custom_notes)
        d_layout.addWidget(notes_in)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save Changes")
        save_btn.clicked.connect(dlg.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        d_layout.addLayout(btn_row)

        if dlg.exec() == QDialog.Accepted:
            try:
                self.whatsapp_service.update_contact(
                    name=contact.name,
                    priority=pri_combo.currentText(),
                    category=cat_combo.currentText(),
                    trusted=trusted_chk.isChecked(),
                    custom_notes=notes_in.text().strip(),
                )
                self.refresh_contacts()
            except Exception as e:
                show_error_message("Edit Error", str(e))

    def _remove_contact(self, name: str):
        self.whatsapp_service.remove_contact(name)
        self.refresh_contacts()

    @asyncSlot()
    async def _analyze_imported_message(self):
        sender = self.sender_in.text().strip() or "Unknown Contact"
        text = self.msg_text_in.toPlainText().strip()
        if not text:
            show_error_message("Validation Error", "Message content cannot be empty.")
            return

        try:
            ai_prov = getattr(self.window(), "ai_provider", None)
            msg = await asyncio.to_thread(
                self.whatsapp_service.import_and_analyze_message, sender, text, ai_prov
            )
            # Sync to Alert Center if alert_service is available on main window
            alert_svc = getattr(self.window(), "alert_service", None)
            if alert_svc:
                matched_contact, contact_pri = self.whatsapp_service.match_contact(sender)
                cat = matched_contact.category if matched_contact else "General"
                alert_svc.add_whatsapp_alert(msg, category=cat, contact_priority=contact_pri)

            self.sender_in.clear()
            self.msg_text_in.clear()
            self.refresh_history()
            show_info_message("Message Analyzed", f"WhatsApp message from '{msg.sender}' analyzed (Urgency: {msg.urgency.upper()}).")
        except Exception as e:
            show_error_message("Analysis Error", str(e))

    def refresh_history(self):
        while self.history_list_layout.count() > 1:
            child = self.history_list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        history = self.whatsapp_service.get_history()
        if not history:
            empty = QLabel("<p style='color: #718096; text-align: center; margin: 30px;'>No message history imported.</p>")
            empty.setAlignment(Qt.AlignCenter)
            self.history_list_layout.insertWidget(0, empty)
            return

        for msg in history:
            card = self._create_message_card(msg)
            self.history_list_layout.insertWidget(self.history_list_layout.count() - 1, card)

    def _create_message_card(self, msg: WhatsAppMessage) -> QWidget:
        card = LiquidGlassCard(self.history_container, corner_radius=12)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        top = QHBoxLayout()
        sender_lbl = QLabel(f"<b style='color: #25D366;'>{msg.sender}</b>")
        time_lbl = QLabel(msg.timestamp)
        time_lbl.setStyleSheet("color: #718096; font-size: 11px;")
        top.addWidget(sender_lbl, stretch=1)
        top.addWidget(time_lbl)

        u_cols = {"critical": "#FF4444", "high": "#FFAA00", "medium": "#00C8FF", "low": "#7FFFD4"}
        u_col = u_cols.get(msg.urgency, "#7FFFD4")
        u_badge = QLabel(f" {msg.urgency.upper()} ")
        u_badge.setStyleSheet(f"color: {u_col}; border: 1px solid {u_col}; border-radius: 4px; font-size: 10px; font-weight: bold;")
        top.addWidget(u_badge)
        layout.addLayout(top)

        txt_lbl = QLabel(f"<span style='color: #E2E8F0; font-size: 12px;'>{msg.message_text}</span>")
        txt_lbl.setWordWrap(True)
        layout.addWidget(txt_lbl)

        sum_lbl = QLabel(f"<i style='color: #A0AEC0; font-size: 11px;'>AI Summary: {msg.summary}</i>")
        sum_lbl.setWordWrap(True)
        layout.addWidget(sum_lbl)

        reply_box = QVBoxLayout()
        if msg.suggested_reply:
            r_lbl = QLabel(f"<b>Suggested Reply:</b> {msg.suggested_reply}")
            r_lbl.setStyleSheet("background: rgba(37, 211, 102, 20); padding: 6px; border-radius: 6px; color: #7FFFD4; font-size: 11px;")
            r_lbl.setWordWrap(True)
            reply_box.addWidget(r_lbl)

        btn_row = QHBoxLayout()
        gen_reply_btn = QPushButton("✍️ AI Generate Reply")
        gen_reply_btn.setFixedHeight(28)
        gen_reply_btn.setCursor(Qt.PointingHandCursor)
        gen_reply_btn.clicked.connect(lambda _, m=msg: self._generate_reply_action(m))
        btn_row.addWidget(gen_reply_btn)

        if msg.suggested_reply:
            copy_btn = QPushButton("📋 Copy Reply to Clipboard")
            copy_btn.setFixedHeight(28)
            copy_btn.setCursor(Qt.PointingHandCursor)
            copy_btn.setStyleSheet("background-color: rgba(0, 200, 255, 50); font-weight: bold;")
            copy_btn.clicked.connect(lambda _, r=msg.suggested_reply: self._copy_reply_action(r))
            btn_row.addWidget(copy_btn)

        reply_box.addLayout(btn_row)
        layout.addLayout(reply_box)

        return card

    @asyncSlot()
    async def _generate_reply_action(self, msg: WhatsAppMessage):
        try:
            ai_prov = getattr(self.window(), "ai_provider", None)
            reply_text = await asyncio.to_thread(
                self.whatsapp_service.generate_suggested_reply, msg, ai_prov
            )
            self.refresh_history()
            show_info_message("Reply Generated", f"AI Suggested Reply generated for '{msg.sender}'. Click 'Copy Reply to Clipboard' to paste into WhatsApp Desktop.")
        except Exception as e:
            show_error_message("Reply Generation Error", str(e))

    def _copy_reply_action(self, text: str):
        ok = self.whatsapp_service.copy_reply_to_clipboard(text)
        if ok:
            show_info_message("Clipboard", "Reply text copied to Windows clipboard! Ready to paste into WhatsApp Desktop.")

    def _clear_history(self):
        reply = QMessageBox.question(
            self,
            "Clear History",
            "Are you sure you want to clear all imported WhatsApp message history?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.whatsapp_service.clear_history()
            self.refresh_history()


# ----------------------------------------------------------------------
# 5. INTELLIGENT WORKFLOW ORCHESTRATION WIDGET
# ----------------------------------------------------------------------

class WorkflowOrchestrationWidget(QWidget):
    """
    Dedicated view for Intelligent Workflow Orchestration.
    Displays preset/custom workflows, step-by-step plans, execution progress,
    pause/resume/cancel controls, and execution history log.
    """

    def __init__(self, workflow_engine: WorkflowEngine, parent=None):
        super().__init__(parent)
        self.workflow_engine = workflow_engine
        self._setup_ui()
        self._connect_engine_signals()
        self.refresh_presets()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        # Header Card
        header_card = LiquidGlassCard(self, corner_radius=18)
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(18, 12, 18, 12)

        title_box = QVBoxLayout()
        title = QLabel("<h2 style='color: #00C8FF; margin: 0;'>Intelligent Workflow Orchestration</h2>")
        desc = QLabel("Combine desktop automation, browser navigation, AI, email, and WhatsApp into structured multi-step workflows")
        desc.setStyleSheet("color: #A0AEC0; font-size: 13px;")
        title_box.addWidget(title)
        title_box.addWidget(desc)
        header_layout.addLayout(title_box)

        header_layout.addStretch()

        self.wf_status_badge = QLabel(" IDLE ")
        self.wf_status_badge.setStyleSheet("background: rgba(0, 200, 255, 30); color: #00C8FF; border: 1px solid #00C8FF; border-radius: 6px; font-weight: bold; padding: 4px 10px;")
        header_layout.addWidget(self.wf_status_badge)

        layout.addWidget(header_card)

        # Main Splitter: Left = Presets & Controls, Right = Active Step Plan & Execution Log
        splitter = QSplitter(Qt.Horizontal, self)

        # Left: Presets Card
        left_card = LiquidGlassCard(self, corner_radius=16)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(16, 12, 16, 12)
        left_layout.setSpacing(10)

        left_layout.addWidget(QLabel("<b style='color: #25D366; font-size: 14px;'>Preset Multi-Step Workflows</b>"))

        self.preset_combo = QComboBox()
        self.preset_combo.setFixedHeight(34)
        left_layout.addWidget(self.preset_combo)

        self.preset_desc_lbl = QLabel("Select a preset workflow to inspect its plan.")
        self.preset_desc_lbl.setStyleSheet("color: #A0AEC0; font-size: 11px;")
        self.preset_desc_lbl.setWordWrap(True)
        left_layout.addWidget(self.preset_desc_lbl)

        ctrl_row = QHBoxLayout()
        self.start_btn = QPushButton("▶ Run Workflow")
        self.start_btn.setFixedHeight(36)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.setStyleSheet("background-color: rgba(0, 200, 255, 60); font-weight: bold;")
        self.start_btn.clicked.connect(self._run_selected_preset)
        ctrl_row.addWidget(self.start_btn)

        self.pause_btn = QPushButton("⏸ Pause")
        self.pause_btn.setFixedHeight(36)
        self.pause_btn.setCursor(Qt.PointingHandCursor)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._toggle_pause_resume)
        ctrl_row.addWidget(self.pause_btn)

        self.cancel_btn = QPushButton("⏹ Cancel")
        self.cancel_btn.setFixedHeight(36)
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setStyleSheet("color: #FF6B6B;")
        self.cancel_btn.clicked.connect(self._cancel_workflow_action)
        ctrl_row.addWidget(self.cancel_btn)

        left_layout.addLayout(ctrl_row)
        left_layout.addStretch()
        splitter.addWidget(left_card)

        # Right: Active Workflow Steps & Log
        right_card = LiquidGlassCard(self, corner_radius=16)
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(16, 12, 16, 12)
        right_layout.setSpacing(8)

        right_layout.addWidget(QLabel("<b style='color: #7FFFD4; font-size: 14px;'>Planned Steps & Progress Log</b>"))

        self.steps_scroll = QScrollArea()
        self.steps_scroll.setWidgetResizable(True)
        self.steps_container = QWidget()
        self.steps_list_layout = QVBoxLayout(self.steps_container)
        self.steps_list_layout.setContentsMargins(0, 0, 0, 0)
        self.steps_list_layout.setSpacing(6)
        self.steps_list_layout.addStretch()
        self.steps_scroll.setWidget(self.steps_container)
        right_layout.addWidget(self.steps_scroll, stretch=1)

        right_layout.addWidget(QLabel("<b style='color: #A0AEC0;'>Workflow Execution Output</b>"))
        self.wf_output_log = QTextEdit()
        self.wf_output_log.setReadOnly(True)
        self.wf_output_log.setFixedHeight(120)
        self.wf_output_log.setStyleSheet("background-color: rgba(10, 15, 30, 200); font-family: Consolas; font-size: 11px;")
        right_layout.addWidget(self.wf_output_log)

        splitter.addWidget(right_card)
        splitter.setSizes([420, 580])
        layout.addWidget(splitter, stretch=1)

    def _connect_engine_signals(self):
        self.workflow_engine.workflow_started.connect(self._on_wf_started)
        self.workflow_engine.step_started.connect(self._on_step_started)
        self.workflow_engine.step_completed.connect(self._on_step_completed)
        self.workflow_engine.step_confirmation_required.connect(self._on_step_confirmation_required)
        self.workflow_engine.workflow_completed.connect(self._on_wf_completed)
        self.workflow_engine.workflow_failed.connect(self._on_wf_failed)
        self.workflow_engine.workflow_paused.connect(self._on_wf_paused)
        self.workflow_engine.workflow_resumed.connect(self._on_wf_resumed)
        self.workflow_engine.workflow_cancelled.connect(self._on_wf_cancelled)

    def refresh_presets(self):
        self.preset_combo.clear()
        self._preset_map = {wf.id: wf for wf in self.workflow_engine.get_preset_workflows()}
        for wf_id, wf in self._preset_map.items():
            self.preset_combo.addItem(wf.title, userData=wf_id)

        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self._on_preset_changed()

    def _on_preset_changed(self):
        wf_id = self.preset_combo.currentData()
        if wf_id in self._preset_map:
            wf = self._preset_map[wf_id]
            self.preset_desc_lbl.setText(wf.description)
            self._render_workflow_steps(wf)

    def _render_workflow_steps(self, wf: Workflow):
        while self.steps_list_layout.count() > 1:
            child = self.steps_list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        for i, step in enumerate(wf.steps):
            row = LiquidGlassCard(self.steps_container, corner_radius=10)
            r_layout = QHBoxLayout(row)
            r_layout.setContentsMargins(10, 8, 10, 8)

            idx_lbl = QLabel(f"<b>#{i+1}</b>")
            idx_lbl.setStyleSheet("color: #00C8FF; font-size: 12px;")
            r_layout.addWidget(idx_lbl)

            act_lbl = QLabel(f"[{step.action_type}]")
            act_lbl.setStyleSheet("color: #7FFFD4; font-size: 11px;")
            r_layout.addWidget(act_lbl)

            desc_lbl = QLabel(step.description)
            desc_lbl.setStyleSheet("color: #F0F4F8; font-size: 12px;")
            r_layout.addWidget(desc_lbl, stretch=1)

            s_cols = {
                "completed": "#7FFFD4",
                "running": "#00C8FF",
                "failed": "#FF4444",
                "cancelled": "#FF6B6B",
                "awaiting_confirmation": "#FFAA00",
                "pending": "#718096",
            }
            scol = s_cols.get(step.status, "#718096")
            badge = QLabel(f" {step.status.upper()} ")
            badge.setStyleSheet(f"color: {scol}; border: 1px solid {scol}; border-radius: 4px; font-size: 10px; font-weight: bold;")
            r_layout.addWidget(badge)

            self.steps_list_layout.insertWidget(self.steps_list_layout.count() - 1, row)

    def _run_selected_preset(self):
        wf_id = self.preset_combo.currentData()
        if wf_id in self._preset_map:
            wf = self._preset_map[wf_id]
            ok = self.workflow_engine.start_workflow(wf)
            if not ok:
                show_error_message("Workflow Engine", "Could not start workflow.")

    def _toggle_pause_resume(self):
        active = self.workflow_engine.get_active_workflow()
        if not active:
            return

        if active.status == "running":
            self.workflow_engine.pause_workflow(active.id)
        elif active.status == "paused":
            self.workflow_engine.resume_workflow(active.id)

    def _cancel_workflow_action(self):
        active = self.workflow_engine.get_active_workflow()
        if active:
            self.workflow_engine.cancel_workflow(active.id)

    # Signal slots
    def _on_wf_started(self, wf_id: str):
        self.wf_status_badge.setText(" RUNNING ")
        self.wf_status_badge.setStyleSheet("background: rgba(0, 200, 255, 40); color: #00C8FF; border: 1px solid #00C8FF; border-radius: 6px; font-weight: bold; padding: 4px 10px;")
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.wf_output_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Started workflow '{wf_id}'")

    def _on_step_started(self, wf_id: str, step_idx: int, desc: str):
        self.wf_output_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Step #{step_idx+1} running: {desc}")
        active = self.workflow_engine.get_active_workflow()
        if active:
            self._render_workflow_steps(active)

    def _on_step_completed(self, wf_id: str, step_idx: int, result_msg: str):
        self.wf_output_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ Step #{step_idx+1} completed: {result_msg}")
        active = self.workflow_engine.get_active_workflow()
        if active:
            self._render_workflow_steps(active)

    def _on_step_confirmation_required(self, wf_id: str, step_idx: int, prompt_msg: str):
        self.wf_output_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Confirmation required for step #{step_idx+1}")
        active = self.workflow_engine.get_active_workflow()
        if active:
            self._render_workflow_steps(active)

        reply = QMessageBox.question(
            self,
            "Workflow Confirmation Required",
            f"{prompt_msg}\n\nDo you approve execution of this step?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        self.workflow_engine.confirm_step_and_continue(wf_id, step_idx, approved=(reply == QMessageBox.Yes))

    def _on_wf_completed(self, wf_id: str, final_msg: str):
        self.wf_status_badge.setText(" COMPLETED ")
        self.wf_status_badge.setStyleSheet("background: rgba(127, 255, 212, 40); color: #7FFFD4; border: 1px solid #7FFFD4; border-radius: 6px; font-weight: bold; padding: 4px 10px;")
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.wf_output_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] 🎉 {final_msg}")

    def _on_wf_failed(self, wf_id: str, err_msg: str):
        self.wf_status_badge.setText(" FAILED ")
        self.wf_status_badge.setStyleSheet("background: rgba(255, 68, 68, 40); color: #FF4444; border: 1px solid #FF4444; border-radius: 6px; font-weight: bold; padding: 4px 10px;")
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.wf_output_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ {err_msg}")

    def _on_wf_paused(self, wf_id: str):
        self.wf_status_badge.setText(" PAUSED ")
        self.wf_status_badge.setStyleSheet("background: rgba(255, 170, 0, 40); color: #FFAA00; border: 1px solid #FFAA00; border-radius: 6px; font-weight: bold; padding: 4px 10px;")
        self.pause_btn.setText("▶ Resume")

    def _on_wf_resumed(self, wf_id: str):
        self.wf_status_badge.setText(" RUNNING ")
        self.wf_status_badge.setStyleSheet("background: rgba(0, 200, 255, 40); color: #00C8FF; border: 1px solid #00C8FF; border-radius: 6px; font-weight: bold; padding: 4px 10px;")
        self.pause_btn.setText("⏸ Pause")

    def _on_wf_cancelled(self, wf_id: str):
        self.wf_status_badge.setText(" CANCELLED ")
        self.wf_status_badge.setStyleSheet("background: rgba(255, 107, 107, 40); color: #FF6B6B; border: 1px solid #FF6B6B; border-radius: 6px; font-weight: bold; padding: 4px 10px;")
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)


# ----------------------------------------------------------------------
# 6. WINDOWS AUTOMATION WIDGET
# ----------------------------------------------------------------------

class AutomationManagementWidget(QWidget):
    """
    Dedicated view for safe Windows & Browser automation:
    Approved applications registry, safe URL launcher, web text extractor,
    window controls, system telemetry, workflows, and execution audit log.
    """

    def __init__(self, automation_service: WindowsAutomationService, browser_service: Optional[BrowserAutomationService] = None, parent=None):
        super().__init__(parent)
        self.automation_service = automation_service
        self.browser_service = browser_service or BrowserAutomationService()
        self._setup_ui()
        self.refresh_system_status()
        self.refresh_audit_log()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        # Header Card
        header_card = LiquidGlassCard(self, corner_radius=18)
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(18, 12, 18, 12)

        title_box = QVBoxLayout()
        title = QLabel("<h2 style='color: #00C8FF; margin: 0;'>Windows Automation</h2>")
        desc = QLabel("Controlled desktop operations, safe application launching, and telemetry")
        desc.setStyleSheet("color: #A0AEC0; font-size: 13px;")
        title_box.addWidget(title)
        title_box.addWidget(desc)
        header_layout.addLayout(title_box)

        header_layout.addStretch()

        self.auto_master_chk = QCheckBox("Enable Desktop Automation")
        self.auto_master_chk.setChecked(self.automation_service.is_automation_enabled())
        self.auto_master_chk.toggled.connect(self._on_toggle_master_automation)
        header_layout.addWidget(self.auto_master_chk)

        refresh_telemetry_btn = QPushButton("🔄 Refresh Telemetry")
        refresh_telemetry_btn.setCursor(Qt.PointingHandCursor)
        refresh_telemetry_btn.clicked.connect(self.refresh_system_status)
        header_layout.addWidget(refresh_telemetry_btn)

        layout.addWidget(header_card)

        # Telemetry Card
        self.telemetry_card = LiquidGlassCard(self, corner_radius=16)
        tele_layout = QVBoxLayout(self.telemetry_card)
        tele_layout.setContentsMargins(18, 12, 18, 12)
        tele_title = QLabel("<b style='color: #7FFFD4;'>System Telemetry</b>")
        self.telemetry_content = QLabel("Collecting system telemetry...")
        self.telemetry_content.setStyleSheet("color: #E2E8F0; font-size: 12px;")
        tele_layout.addWidget(tele_title)
        tele_layout.addWidget(self.telemetry_content)
        layout.addWidget(self.telemetry_card)

        # Middle Section Splitter: Approved Apps & Safe Workflows
        mid_splitter = QSplitter(Qt.Horizontal, self)

        # Approved Apps Card
        apps_card = LiquidGlassCard(self, corner_radius=18)
        apps_layout = QVBoxLayout(apps_card)
        apps_layout.setContentsMargins(16, 12, 16, 12)
        apps_layout.setSpacing(8)

        apps_layout.addWidget(QLabel("<b style='color: #00C8FF; font-size: 14px;'>Approved Applications Allowlist</b>"))

        apps_scroll = QScrollArea()
        apps_scroll.setWidgetResizable(True)
        apps_content = QWidget()
        apps_grid = QVBoxLayout(apps_content)
        apps_grid.setContentsMargins(0, 0, 0, 0)
        apps_grid.setSpacing(6)

        for app in self.automation_service.get_allowed_apps():
            row = LiquidGlassCard(apps_content, corner_radius=10)
            r_layout = QHBoxLayout(row)
            r_layout.setContentsMargins(10, 8, 10, 8)

            txt = QVBoxLayout()
            name_lbl = QLabel(f"<b>{app.display_name}</b> ({app.executable})")
            name_lbl.setStyleSheet("color: #F0F4F8; font-size: 12px;")
            desc_lbl = QLabel(app.description)
            desc_lbl.setStyleSheet("color: #A0AEC0; font-size: 10px;")
            txt.addWidget(name_lbl)
            txt.addWidget(desc_lbl)
            r_layout.addLayout(txt, stretch=1)

            launch_btn = QPushButton("Launch")
            launch_btn.setFixedSize(70, 28)
            launch_btn.setCursor(Qt.PointingHandCursor)
            launch_btn.clicked.connect(lambda _, aid=app.id: self._launch_app_action(aid))
            r_layout.addWidget(launch_btn)

            apps_grid.addWidget(row)

        apps_grid.addStretch()
        apps_scroll.setWidget(apps_content)
        apps_layout.addWidget(apps_scroll)
        mid_splitter.addWidget(apps_card)

        # Right: URL Launcher, Path Opener, Web Search & Window Controls
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        # URL Launcher
        url_card = LiquidGlassCard(right_panel, corner_radius=16)
        url_layout = QVBoxLayout(url_card)
        url_layout.setContentsMargins(14, 10, 14, 10)
        url_layout.addWidget(QLabel("<b style='color: #7FFFD4;'>Safe Browser URL Launcher</b>"))

        url_row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://example.com...")
        self.url_input.setFixedHeight(32)
        url_row.addWidget(self.url_input, stretch=3)

        open_url_btn = QPushButton("Open URL")
        open_url_btn.setFixedHeight(32)
        open_url_btn.setCursor(Qt.PointingHandCursor)
        open_url_btn.clicked.connect(self._open_url_action)
        url_row.addWidget(open_url_btn, stretch=1)
        url_layout.addLayout(url_row)
        right_layout.addWidget(url_card)

        # File/Folder Opener Card
        path_card = LiquidGlassCard(right_panel, corner_radius=16)
        p_layout = QVBoxLayout(path_card)
        p_layout.setContentsMargins(14, 10, 14, 10)
        p_layout.addWidget(QLabel("<b style='color: #00C8FF;'>Open Local File or Folder</b>"))

        p_row = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Local path (e.g. C:\\Users\\... or /home/...)...")
        self.path_input.setFixedHeight(32)
        p_row.addWidget(self.path_input, stretch=3)

        open_p_btn = QPushButton("Open Path")
        open_p_btn.setFixedHeight(32)
        open_p_btn.setCursor(Qt.PointingHandCursor)
        open_p_btn.clicked.connect(self._open_path_action)
        p_row.addWidget(open_p_btn, stretch=1)
        p_layout.addLayout(p_row)
        right_layout.addWidget(path_card)

        # Web Search & Window Controls Row
        ctrl_row = QHBoxLayout()

        search_card = LiquidGlassCard(right_panel, corner_radius=16)
        s_layout = QVBoxLayout(search_card)
        s_layout.setContentsMargins(12, 10, 12, 10)
        s_layout.addWidget(QLabel("<b style='color: #7FFFD4;'>Web Search</b>"))
        self.web_search_input = QLineEdit()
        self.web_search_input.setPlaceholderText("Search terms...")
        self.web_search_input.setFixedHeight(30)
        self.web_search_input.returnPressed.connect(self._web_search_action)
        s_layout.addWidget(self.web_search_input)
        web_search_btn = QPushButton("🔍 Search")
        web_search_btn.setFixedHeight(28)
        web_search_btn.clicked.connect(self._web_search_action)
        s_layout.addWidget(web_search_btn)
        ctrl_row.addWidget(search_card, stretch=1)

        window_card = LiquidGlassCard(right_panel, corner_radius=16)
        w_layout = QVBoxLayout(window_card)
        w_layout.setContentsMargins(12, 10, 12, 10)
        w_layout.addWidget(QLabel("<b style='color: #00C8FF;'>Window Control</b>"))
        w_row = QHBoxLayout()
        self.window_act_combo = QComboBox()
        self.window_act_combo.addItems(["minimize", "maximize", "restore", "focus"])
        self.window_act_combo.setFixedHeight(30)
        w_row.addWidget(self.window_act_combo)
        self.window_app_combo = QComboBox()
        self.window_app_combo.addItems(list(APPROVED_APPS.keys()))
        self.window_app_combo.setFixedHeight(30)
        w_row.addWidget(self.window_app_combo)
        w_layout.addLayout(w_row)
        win_exec_btn = QPushButton("Execute")
        win_exec_btn.setFixedHeight(28)
        win_exec_btn.clicked.connect(self._window_control_action)
        w_layout.addWidget(win_exec_btn)
        ctrl_row.addWidget(window_card, stretch=1)

        right_layout.addLayout(ctrl_row)

        # Preset Workflows
        wf_card = LiquidGlassCard(right_panel, corner_radius=16)
        wf_layout = QVBoxLayout(wf_card)
        wf_layout.setContentsMargins(14, 10, 14, 10)
        wf_layout.addWidget(QLabel("<b style='color: #00C8FF;'>Preset Safe Workflows</b>"))

        for wf_id, wf_data in self.automation_service.get_registered_workflows().items():
            wf_row = QHBoxLayout()
            w_txt = QVBoxLayout()
            w_txt.addWidget(QLabel(f"<b>{wf_data['name']}</b>"))
            w_txt.addWidget(QLabel(f"<span style='color: #A0AEC0; font-size: 11px;'>{wf_data['description']}</span>"))
            wf_row.addLayout(w_txt, stretch=1)

            run_btn = QPushButton("Execute")
            run_btn.setFixedSize(70, 28)
            run_btn.setCursor(Qt.PointingHandCursor)
            run_btn.clicked.connect(lambda _, wid=wf_id: self._run_workflow_action(wid))
            wf_row.addWidget(run_btn)
            wf_layout.addLayout(wf_row)

        right_layout.addWidget(wf_card, stretch=1)
        mid_splitter.addWidget(right_panel)

        layout.addWidget(mid_splitter, stretch=2)

        # Intelligent Browser Automation Card
        browser_card = LiquidGlassCard(self, corner_radius=16)
        b_layout = QVBoxLayout(browser_card)
        b_layout.setContentsMargins(14, 10, 14, 10)
        b_layout.setSpacing(8)

        b_header = QHBoxLayout()
        b_header.addWidget(QLabel("<b style='color: #7FFFD4; font-size: 14px;'>Intelligent Browser Automation & Web Text Extractor</b>"), stretch=1)
        self.browser_master_chk = QCheckBox("Enable Browser Automation")
        self.browser_master_chk.setChecked(self.browser_service.is_browser_automation_enabled())
        self.browser_master_chk.toggled.connect(self._on_toggle_browser_automation)
        b_header.addWidget(self.browser_master_chk)
        b_layout.addLayout(b_header)

        extract_row = QHBoxLayout()
        self.extract_url_in = QLineEdit()
        self.extract_url_in.setPlaceholderText("https://example.com (Extract readable page text)...")
        self.extract_url_in.setFixedHeight(32)
        extract_row.addWidget(self.extract_url_in, stretch=3)

        extract_btn = QPushButton("✨ Extract Web Page Text")
        extract_btn.setFixedHeight(32)
        extract_btn.setCursor(Qt.PointingHandCursor)
        extract_btn.clicked.connect(self._extract_web_text_action)
        extract_row.addWidget(extract_btn, stretch=1)
        b_layout.addLayout(extract_row)

        self.extracted_display = QTextEdit()
        self.extracted_display.setReadOnly(True)
        self.extracted_display.setPlaceholderText("Extracted webpage text preview...")
        self.extracted_display.setFixedHeight(90)
        b_layout.addWidget(self.extracted_display)

        layout.addWidget(browser_card)

        # Audit Log Card
        audit_card = LiquidGlassCard(self, corner_radius=16)
        audit_layout = QVBoxLayout(audit_card)
        audit_layout.setContentsMargins(14, 10, 14, 10)
        audit_layout.addWidget(QLabel("<b style='color: #A0AEC0;'>Automation Security Audit Log</b>"))

        self.audit_display = QTextEdit()
        self.audit_display.setReadOnly(True)
        self.audit_display.setFixedHeight(110)
        self.audit_display.setStyleSheet("background-color: rgba(10, 15, 30, 200); font-family: Consolas; font-size: 11px;")
        audit_layout.addWidget(self.audit_display)

        layout.addWidget(audit_card, stretch=1)

    def refresh_system_status(self):
        res = self.automation_service.get_system_status()
        if res.success:
            self.telemetry_content.setText(res.message)

    def refresh_audit_log(self):
        logs = self.automation_service.get_audit_log()
        lines = []
        for log in reversed(logs):
            status = "SUCCESS" if log.success else "DENIED"
            lines.append(f"[{log.timestamp[:19]}] [{status}] {log.action}: {log.message}")
        self.audit_display.setText("\n".join(lines))

    def _launch_app_action(self, app_id: str):
        # First attempt without confirmation
        res = self.automation_service.launch_app(app_id, user_confirmed=False)
        if not res.success and res.data and res.data.get("needs_confirmation"):
            reply = QMessageBox.question(
                self,
                "Confirmation Required",
                f"Confirm launching '{app_id}'?\n\n{res.message}",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                res = self.automation_service.launch_app(app_id, user_confirmed=True)

        if res.success:
            show_info_message("Automation", res.message)
        else:
            show_error_message("Automation", res.message)
        self.refresh_audit_log()

    def _on_toggle_master_automation(self, checked: bool):
        self.automation_service.set_automation_enabled(checked)
        show_info_message("Desktop Automation", f"Desktop automation is now {'enabled' if checked else 'disabled'}.")

    def _on_toggle_browser_automation(self, checked: bool):
        self.browser_service.set_browser_automation_enabled(checked)
        show_info_message("Browser Automation", f"Browser automation is now {'enabled' if checked else 'disabled'}.")

    @asyncSlot()
    async def _extract_web_text_action(self):
        url = self.extract_url_in.text().strip()
        if not url:
            show_error_message("Validation Error", "URL cannot be blank.")
            return

        self.extracted_display.setText("Fetching webpage and extracting readable text...")
        res = await asyncio.to_thread(self.browser_service.extract_page_text, url)
        if res.success and res.data:
            text = res.data.get("extracted_text", "")
            self.extracted_display.setText(text)
            show_info_message("Text Extracted", f"Successfully extracted text from {url}")
        else:
            self.extracted_display.setText(f"Extraction Error: {res.message}")
            show_error_message("Extraction Error", res.message)
        self.refresh_audit_log()

    def _open_url_action(self):
        url = self.url_input.text().strip()
        if not url:
            show_error_message("Validation Error", "URL cannot be blank.")
            return

        res = self.automation_service.open_url(url, user_confirmed=True)
        if res.success:
            self.url_input.clear()
            show_info_message("URL Launcher", res.message)
        else:
            show_error_message("Security Rejection", res.message)
        self.refresh_audit_log()

    def _open_path_action(self):
        path_str = self.path_input.text().strip()
        if not path_str:
            show_error_message("Validation Error", "Path cannot be blank.")
            return

        reply = QMessageBox.question(
            self,
            "Confirm Open Path",
            f"Confirm opening local file or folder?\n\nPath: {path_str}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            res = self.automation_service.open_path(path_str, user_confirmed=True)
            if res.success:
                self.path_input.clear()
                show_info_message("Path Opener", res.message)
            else:
                show_error_message("Security Rejection", res.message)
            self.refresh_audit_log()

    def _web_search_action(self):
        q = self.web_search_input.text().strip()
        if not q:
            show_error_message("Validation Error", "Search query cannot be blank.")
            return

        res = self.automation_service.web_search(q, user_confirmed=True)
        if res.success:
            self.web_search_input.clear()
            show_info_message("Web Search", res.message)
        else:
            show_error_message("Error", res.message)
        self.refresh_audit_log()

    def _window_control_action(self):
        act = self.window_act_combo.currentText()
        app_id = self.window_app_combo.currentText()

        reply = QMessageBox.question(
            self,
            "Confirm Window Action",
            f"Confirm window action '{act}' for application '{app_id}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            res = self.automation_service.window_management(act, app_id, user_confirmed=True)
            if res.success:
                show_info_message("Window Management", res.message)
            else:
                show_error_message("Window Action Error", res.message)
            self.refresh_audit_log()

    def _run_workflow_action(self, wf_id: str):
        workflows = self.automation_service.get_registered_workflows()
        workflow = workflows.get(wf_id, {})
        steps = workflow.get("steps", [])
        step_text = "\n".join(
            f"{index + 1}. {step.get('action', 'unknown')}: "
            f"{step.get('app_id') or step.get('url') or step.get('query') or ''}"
            for index, step in enumerate(steps)
        )
        reply = QMessageBox.question(
            self,
            "Confirm Workflow",
            f"Run workflow '{workflow.get('name', wf_id)}'?\n\n"
            f"{step_text}\n\nNo step runs until you approve.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        res = self.automation_service.execute_workflow(wf_id, user_confirmed=True)
        if res.success:
            show_info_message("Workflow Complete", res.message)
        else:
            show_error_message("Workflow Error", res.message)
        self.refresh_audit_log()
