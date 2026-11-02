import asyncio
import html
import json
import logging
from pathlib import Path
import sys

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from ai_core_visualization import AICoreVisualization
from automation_service import WindowsAutomationService
from browser_service import BrowserAutomationService
from config import (
    APP_DIR,
    SecretStorageError,
    get_config_value,
    get_secret,
    load_config,
    save_config,
    set_secret,
)
from engine.agent_loop import AgentRunResult, AgentState
from engine.agent_service import (
    AgentService,
    AgentServiceBusyError,
    AgentServiceError,
    AgentServiceUnavailable,
)
from gemini_api import DEFAULT_MODEL, SUPPORTED_MODELS, GeminiAIProvider
from gmail_service import GmailService
from gui_components import ANIME_CYBERPUNK_QSS, LiquidGlassBackground, LiquidGlassCard
from memory_service import MemoryService
from microphone_service import MicrophoneService, transcribe_audio_bytes
from task_service import TaskService
from alert_service import AlertNotificationService
from workflow_engine import WorkflowEngine
from ui_views import (
    AutomationManagementWidget,
    EmailManagementWidget,
    MainDashboardWidget,
    MemoryManagementWidget,
    NotificationCenterWidget,
    TaskManagementWidget,
    WhatsAppCompanionWidget,
    WorkflowOrchestrationWidget,
)
from whatsapp_service import WhatsAppNotificationListener, WhatsAppService
from utils import show_error_message, show_info_message
from voice_service import VoiceManager

logger = logging.getLogger(__name__)

MASKED_KEY = "********"
MAX_CONVERSATION_MESSAGES = 200


class JarvisMainWindow(QMainWindow):
    """
    The main window for S.E.N.T.I.A.L. Desktop Assistant, featuring an anime-inspired cyberpunk UI.
    Houses Home Dashboard, Voice Chat, Alerts, Workflows, Email, WhatsApp, Automation, Tasks, Memory, and Settings views.
    """

    ai_state_changed = Signal(str)  # Updates the AI core visualization state

    def __init__(self):
        super().__init__()
        self.setWindowTitle("S.E.N.T.I.A.L. | Personal Desktop Intelligence")
        self.resize(1240, 810)
        self.setMinimumSize(1024, 768)

        # Initialize Services
        self.ai_provider = GeminiAIProvider()
        self.alert_service = AlertNotificationService()
        self.task_service = TaskService()
        self.memory_service = MemoryService()
        self.gmail_service = GmailService()
        self.whatsapp_service = WhatsAppService()
        self.whatsapp_listener = WhatsAppNotificationListener(
            self.whatsapp_service, ai_provider=self.ai_provider, parent=self
        )
        self.automation_service = WindowsAutomationService()
        self.browser_service = BrowserAutomationService()
        self.workflow_engine = WorkflowEngine(
            automation_service=self.automation_service,
            browser_service=self.browser_service,
            gmail_service=self.gmail_service,
            whatsapp_service=self.whatsapp_service,
            alert_service=self.alert_service,
            task_service=self.task_service,
            ai_provider=self.ai_provider,
            parent=self,
        )
        self.voice_manager = VoiceManager()
        self.mic_service = MicrophoneService(self)

        # Phase 3: one shared agent service owns the tool registry, the Qt
        # confirmation bridge and the currently active AgentRunner.
        try:
            self.agent_service = AgentService(
                ai_provider=self.ai_provider,
                task_service=self.task_service,
                memory_service=self.memory_service,
                automation_service=self.automation_service,
                browser_service=self.browser_service,
                parent=self,
            )
            self.agent_service.event_received.connect(self._on_agent_event)
            self.agent_service.confirmation_requested.connect(
                self._on_agent_confirmation_requested
            )
        except Exception:
            logger.exception("Agent service initialization failed.")
            self.agent_service = None

        self.conversation_history = []  # list of (role, text) tuples, role in {"User", "S.E.N.T.I.A.L."}
        self._busy = False
        self._sidebar_buttons = []
        self._agent_run_active = False
        self._active_confirmation_box = None
        self._active_confirmation_id = None
        self._agent_fallback_notice_shown = False

        # Timer to reset core visualization state back to idle
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(lambda: self.ai_state_changed.emit("idle"))

        self._setup_ui()
        self._load_settings()

        self.ai_state_changed.emit("idle")
        logger.info("S.E.N.T.I.A.L. application initialized successfully.")

    # ------------------------------------------------------------------ UI Setup

    def _setup_ui(self):
        """Initializes the S.E.N.T.I.A.L. Anime Cyberpunk GUI components and layout."""
        self.background_widget = LiquidGlassBackground(self)
        self.setCentralWidget(self.background_widget)

        self.root_layout = QHBoxLayout(self.background_widget)
        self.root_layout.setContentsMargins(15, 15, 15, 15)
        self.root_layout.setSpacing(15)

        # --- Left sidebar (navigation) ---
        self.sidebar = LiquidGlassCard(self.background_widget, corner_radius=20, border_color=QColor(255, 42, 109, 120))
        self.sidebar.setFixedWidth(210)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(12, 20, 12, 18)
        sidebar_layout.setSpacing(8)

        # App Title in Sidebar
        app_title = QLabel("S.E.N.T.I.A.L.")
        app_title.setObjectName("hdrAnimeTitle")
        app_title.setAlignment(Qt.AlignCenter)
        app_title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        app_title.setStyleSheet("color: #FF2A6D; margin-bottom: 10px; letter-spacing: 2px;")
        sidebar_layout.addWidget(app_title)

        nav_items = [
            ("🏠 Dashboard", 0),
            ("💬 Chat", 1),
            ("🔔 Alerts", 2),
            ("⚡ Workflows", 3),
            ("📧 Email", 4),
            ("📱 WhatsApp", 5),
            ("⚙️ Automation", 6),
            ("📋 Tasks", 7),
            ("🧠 Memory", 8),
            ("⚙️ Settings", 9),
        ]

        for text, index in nav_items:
            btn = self._add_sidebar_button(text, index)
            self._sidebar_buttons.append(btn)

        sidebar_layout.addStretch()

        # Version stamp
        ver_lbl = QLabel("v2.5 S.E.N.T.I.A.L.")
        ver_lbl.setAlignment(Qt.AlignCenter)
        ver_lbl.setStyleSheet("color: #05D9E8; font-size: 11px; font-weight: bold;")
        sidebar_layout.addWidget(ver_lbl)

        self.root_layout.addWidget(self.sidebar)

        # --- Main content area ---
        self.content_area = QWidget(self.background_widget)
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)

        self.stacked_widget = QStackedWidget(self.content_area)
        self.content_layout.addWidget(self.stacked_widget)

        # 0: Main Home Dashboard View
        self.dashboard_view = MainDashboardWidget(self.content_area)
        self.dashboard_view.quick_action_triggered.connect(self.stacked_widget.setCurrentIndex)
        self.stacked_widget.addWidget(self.dashboard_view)

        # 1: Chat View
        self._setup_chat_view()
        self.stacked_widget.addWidget(self.chat_view)

        # 2: Alert Notification Center View
        self.alert_view = NotificationCenterWidget(self.alert_service, self.content_area)
        self.stacked_widget.addWidget(self.alert_view)

        # 3: Intelligent Workflow Orchestration View
        self.workflow_view = WorkflowOrchestrationWidget(self.workflow_engine, self.content_area)
        self.stacked_widget.addWidget(self.workflow_view)

        # 4: Email View
        self.email_view = EmailManagementWidget(self.gmail_service, self.content_area)
        self.stacked_widget.addWidget(self.email_view)

        # 5: WhatsApp Personal Companion View
        self.whatsapp_view = WhatsAppCompanionWidget(
            self.whatsapp_service,
            notification_listener=self.whatsapp_listener,
            parent=self.content_area,
        )
        self.stacked_widget.addWidget(self.whatsapp_view)

        # 6: Windows & Browser Automation View
        self.automation_view = AutomationManagementWidget(
            self.automation_service,
            browser_service=self.browser_service,
            parent=self.content_area,
        )
        self.stacked_widget.addWidget(self.automation_view)

        # 7: Task Management View
        self.task_view = TaskManagementWidget(self.task_service, self.content_area)
        self.stacked_widget.addWidget(self.task_view)

        # 8: Memory & Personalization View
        self.memory_view = MemoryManagementWidget(self.memory_service, self.content_area)
        self.stacked_widget.addWidget(self.memory_view)

        # 9: Settings View
        self._setup_settings_view()
        self.stacked_widget.addWidget(self.settings_view)

        self.stacked_widget.currentChanged.connect(self._on_page_changed)
        self.root_layout.addWidget(self.content_area, stretch=1)

        # Set initial active tab
        self._update_sidebar_styles(0)

        # Apply Anime Cyberpunk QSS Style
        self.setStyleSheet(ANIME_CYBERPUNK_QSS)

    def _add_sidebar_button(self, text, page_index):
        """Creates a styled sidebar navigation button."""
        button = QPushButton(text)
        button.setFixedHeight(42)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(page_index))
        self.sidebar.layout().addWidget(button)
        return button

    def _on_page_changed(self, index):
        """Updates active sidebar button styling and refreshes view data."""
        self._update_sidebar_styles(index)
        if index == 0:
            self.dashboard_view.update_indicators(
                "Ready" if self.ai_provider.is_configured() else "Unconfigured",
                self.voice_manager.get_status()
            )
        elif index == 2:
            self.alert_view.refresh_alerts()
        elif index == 3:
            self.workflow_view.refresh_presets()
        elif index == 4:
            self.email_view.update_auth_status()
        elif index == 5:
            self.whatsapp_view.refresh_all()
        elif index == 6:
            self.automation_view.refresh_system_status()
            self.automation_view.refresh_audit_log()
        elif index == 7:
            self.task_view.refresh_tasks()
        elif index == 8:
            self.memory_view.refresh_memories()

    def _update_sidebar_styles(self, active_index):
        """Highlights the active sidebar navigation tab."""
        for i, btn in enumerate(self._sidebar_buttons):
            if i == active_index:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: rgba(0, 200, 255, 70);
                        border: 1px solid rgba(0, 200, 255, 180);
                        color: #FFFFFF;
                        font-weight: bold;
                    }
                """)
            else:
                btn.setStyleSheet("")

    def _setup_chat_view(self):
        """Sets up the main interactive chat interface."""
        self.chat_view = QWidget(self.content_area)
        chat_layout = QVBoxLayout(self.chat_view)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(15)

        # Top section: Core animation & status card
        top_panel = QWidget(self.chat_view)
        top_panel_layout = QHBoxLayout(top_panel)
        top_panel_layout.setContentsMargins(0, 0, 0, 0)
        top_panel_layout.setSpacing(15)

        self.ai_core_viz = AICoreVisualization(self.chat_view)
        self.ai_core_viz.setFixedSize(220, 220)
        top_panel_layout.addWidget(self.ai_core_viz, alignment=Qt.AlignCenter)
        self.ai_state_changed.connect(self.ai_core_viz.set_state)

        status_card = LiquidGlassCard(self.chat_view, corner_radius=24)
        status_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(20, 15, 20, 15)

        status_header = QLabel("<h3 style='color: #00C8FF; margin: 0;'>System Overview</h3>")
        status_layout.addWidget(status_header)

        self.ai_status_label = QLabel("AI Engine: Initializing...")
        self.ai_status_label.setFont(QFont("Segoe UI", 10))
        self.model_status_label = QLabel(f"Active Model: {DEFAULT_MODEL}")
        self.model_status_label.setFont(QFont("Segoe UI", 10))
        self.agent_progress_label = QLabel("Agent: Idle")
        self.agent_progress_label.setWordWrap(True)
        self.agent_progress_label.setStyleSheet("color: #A0AEC0; font-size: 11px;")

        status_layout.addWidget(self.ai_status_label)
        status_layout.addWidget(self.model_status_label)
        status_layout.addWidget(self.agent_progress_label)

        # Clear chat history button in status card (does NOT touch tasks or persistent memories)
        self.clear_chat_button = QPushButton("Clear Chat History")
        self.clear_chat_button.setFixedHeight(32)
        self.clear_chat_button.setCursor(Qt.PointingHandCursor)
        self.clear_chat_button.clicked.connect(self._clear_chat_history)
        status_layout.addWidget(self.clear_chat_button)

        status_layout.addStretch()

        top_panel_layout.addWidget(status_card)
        chat_layout.addWidget(top_panel)

        # Conversation display widget
        self.conversation_display = QTextEdit(self.chat_view)
        self.conversation_display.setReadOnly(True)
        self.conversation_display.setFont(QFont("Segoe UI", 10))
        self.conversation_display.setPlaceholderText("S.E.N.T.I.A.L. is ready. Ask me anything!")
        chat_layout.addWidget(self.conversation_display, stretch=1)

        # Command input row
        input_panel = QWidget(self.chat_view)
        input_layout = QHBoxLayout(input_panel)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(10)

        self.mic_button = QPushButton("🎤")
        self.mic_button.setFixedSize(42, 42)
        self.mic_button.setCursor(Qt.PointingHandCursor)
        self.mic_button.setToolTip("Click to start/stop voice recording")
        self.mic_button.clicked.connect(self._toggle_microphone_recording)
        input_layout.addWidget(self.mic_button)

        self.command_input = QLineEdit(self.chat_view)
        self.command_input.setPlaceholderText("Ask S.E.N.T.I.A.L. or type a command...")
        self.command_input.setFont(QFont("Segoe UI", 10))
        self.command_input.setFixedHeight(42)
        self.command_input.returnPressed.connect(self._send_command)
        input_layout.addWidget(self.command_input)

        self.send_button = QPushButton("Send")
        self.send_button.setFixedSize(90, 42)
        self.send_button.setCursor(Qt.PointingHandCursor)
        self.send_button.clicked.connect(self._send_command)
        input_layout.addWidget(self.send_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setFixedSize(90, 42)
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.setToolTip("Cancel the active agent run")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_agent_run)
        input_layout.addWidget(self.cancel_button)

        chat_layout.addWidget(input_panel)

    def _setup_settings_view(self):
        """Sets up comprehensive configuration settings for API keys, models, memory, automation, and voice."""
        self.settings_view = QWidget(self.content_area)
        settings_layout = QVBoxLayout(self.settings_view)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(15)

        scroll_area = QScrollArea(self.settings_view)
        scroll_area.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(15)
        scroll_area.setWidget(scroll_content)
        settings_layout.addWidget(scroll_area)

        # 1. Gemini API Config Card
        gemini_card = LiquidGlassCard(scroll_content, corner_radius=20)
        gemini_layout = QVBoxLayout(gemini_card)
        gemini_layout.setContentsMargins(20, 16, 20, 16)
        scroll_layout.addWidget(gemini_card)

        gemini_layout.addWidget(QLabel("<h3 style='color: #00C8FF; margin: 0;'>Google Gemini AI Configuration</h3>"))

        gemini_layout.addWidget(QLabel("Gemini API Key (stored securely in OS Keyring / Credential Manager):"))
        self.gemini_api_key_input = QLineEdit()
        self.gemini_api_key_input.setEchoMode(QLineEdit.Password)
        self.gemini_api_key_input.setPlaceholderText("Enter AI Studio API Key (AIzaSy...)")
        gemini_layout.addWidget(self.gemini_api_key_input)

        gemini_layout.addWidget(QLabel("Active Gemini Model:"))
        self.gemini_model_selector = QComboBox()
        self.gemini_model_selector.setEditable(True)
        self.gemini_model_selector.addItems(SUPPORTED_MODELS)
        gemini_layout.addWidget(self.gemini_model_selector)

        self.save_key_button = QPushButton("Save & Validate Credentials")
        self.save_key_button.setFixedHeight(40)
        self.save_key_button.setCursor(Qt.PointingHandCursor)
        self.save_key_button.clicked.connect(self._save_and_test_gemini_key)
        gemini_layout.addWidget(self.save_key_button)

        # 2. Personalization & Memory Card
        mem_card = LiquidGlassCard(scroll_content, corner_radius=20)
        mem_layout = QVBoxLayout(mem_card)
        mem_layout.setContentsMargins(20, 16, 20, 16)
        scroll_layout.addWidget(mem_card)

        mem_layout.addWidget(QLabel("<h3 style='color: #7FFFD4; margin: 0;'>Memory & Context Injection</h3>"))
        self.mem_enabled_checkbox = QCheckBox("Enable long-term memory and user preferences injection into AI prompts")
        self.mem_enabled_checkbox.setChecked(self.memory_service.is_memory_enabled())
        self.mem_enabled_checkbox.toggled.connect(self._on_memory_checkbox_toggled)
        mem_layout.addWidget(self.mem_enabled_checkbox)

        # 3. Windows Automation Security Card
        auto_card = LiquidGlassCard(scroll_content, corner_radius=20)
        auto_layout = QVBoxLayout(auto_card)
        auto_layout.setContentsMargins(20, 16, 20, 16)
        scroll_layout.addWidget(auto_card)

        auto_layout.addWidget(QLabel("<h3 style='color: #FFAA00; margin: 0;'>Automation Security</h3>"))
        self.confirm_auto_checkbox = QCheckBox("Require explicit confirmation before launching applications or workflows")
        self.confirm_auto_checkbox.setChecked(bool(get_config_value("automation_require_confirmation", True)))
        self.confirm_auto_checkbox.toggled.connect(self._on_automation_confirm_toggled)
        auto_layout.addWidget(self.confirm_auto_checkbox)

        # 4. Voice Controls Card
        voice_card = LiquidGlassCard(scroll_content, corner_radius=20)
        voice_layout = QVBoxLayout(voice_card)
        voice_layout.setContentsMargins(20, 16, 20, 16)
        scroll_layout.addWidget(voice_card)

        voice_layout.addWidget(QLabel("<h3 style='color: #00C8FF; margin: 0;'>Voice Engine Configuration</h3>"))
        voice_row = QHBoxLayout()
        self.voice_combo = QComboBox()
        self.voice_combo.addItems(["Disabled", "ElevenLabs TTS (Voice Clone)", "Windows SAPI (Native TTS)"])
        current_voice = get_config_value("voice_provider", "disabled")
        if current_voice == "elevenlabs":
            self.voice_combo.setCurrentText("ElevenLabs TTS (Voice Clone)")
        elif current_voice == "windows_sapi":
            self.voice_combo.setCurrentText("Windows SAPI (Native TTS)")
        else:
            self.voice_combo.setCurrentText("Disabled")
        self.voice_combo.currentIndexChanged.connect(self._on_voice_provider_changed)
        voice_row.addWidget(self.voice_combo, stretch=2)

        test_voice_btn = QPushButton("🔊 Test Voice")
        test_voice_btn.setFixedHeight(36)
        test_voice_btn.setCursor(Qt.PointingHandCursor)
        test_voice_btn.clicked.connect(self._test_voice)
        voice_row.addWidget(test_voice_btn, stretch=1)

        stop_voice_btn = QPushButton("⏹ Stop Audio")
        stop_voice_btn.setFixedHeight(36)
        stop_voice_btn.setCursor(Qt.PointingHandCursor)
        stop_voice_btn.clicked.connect(self.voice_manager.stop)
        voice_row.addWidget(stop_voice_btn, stretch=1)
        voice_layout.addLayout(voice_row)

        voice_layout.addWidget(QLabel("ElevenLabs API Key:"))
        self.elevenlabs_key_input = QLineEdit()
        self.elevenlabs_key_input.setEchoMode(QLineEdit.Password)
        self.elevenlabs_key_input.setPlaceholderText("Enter ElevenLabs API Key...")
        existing_el_key = get_secret("elevenlabs_api_key")
        if existing_el_key:
            self.elevenlabs_key_input.setText(MASKED_KEY)
        voice_layout.addWidget(self.elevenlabs_key_input)

        voice_layout.addWidget(QLabel("ElevenLabs Voice ID:"))
        self.elevenlabs_voice_id_input = QLineEdit()
        self.elevenlabs_voice_id_input.setPlaceholderText("Enter ElevenLabs Voice ID (e.g. 21m00Tcm4TlvDq8ikWAM)...")
        existing_voice_id = get_secret("elevenlabs_voice_id") or get_config_value("elevenlabs_voice_id", "")
        self.elevenlabs_voice_id_input.setText(existing_voice_id)
        voice_layout.addWidget(self.elevenlabs_voice_id_input)

        save_voice_settings_btn = QPushButton("Save Voice Configuration")
        save_voice_settings_btn.setFixedHeight(36)
        save_voice_settings_btn.setCursor(Qt.PointingHandCursor)
        save_voice_settings_btn.clicked.connect(self._save_elevenlabs_credentials)
        voice_layout.addWidget(save_voice_settings_btn)

        self.voice_status_lbl = QLabel(f"Voice Status: {self.voice_manager.get_status()}")
        self.voice_status_lbl.setStyleSheet("color: #A0AEC0; font-size: 11px;")
        voice_layout.addWidget(self.voice_status_lbl)

        # 5. Environment & Storage Card
        storage_card = LiquidGlassCard(scroll_content, corner_radius=20)
        storage_layout = QVBoxLayout(storage_card)
        storage_layout.setContentsMargins(20, 16, 20, 16)
        scroll_layout.addWidget(storage_card)

        storage_layout.addWidget(QLabel("<h3 style='color: #A0AEC0; margin: 0;'>Storage & Data Locations</h3>"))
        storage_info = QLabel(
            f"<b>Application Data:</b> <code>{APP_DIR}</code><br>"
            f"<b>Tasks Database:</b> <code>{APP_DIR / 'tasks.db'}</code><br>"
            f"<b>Memory Database:</b> <code>{APP_DIR / 'memory.db'}</code><br>"
            f"<b>Gmail Credentials:</b> <code>{self.gmail_service.get_credentials_path()}</code>"
        )
        storage_info.setTextFormat(Qt.RichText)
        storage_info.setStyleSheet("color: #718096; font-size: 11px; line-height: 1.5;")
        storage_layout.addWidget(storage_info)

        scroll_layout.addStretch()

    # ------------------------------------------------------------------ Logic & Settings Handlers

    def _on_memory_checkbox_toggled(self, checked: bool):
        self.memory_service.set_memory_enabled(checked)
        self.memory_view.memory_toggle.setChecked(checked)

    def _on_automation_confirm_toggled(self, checked: bool):
        save_config({"automation_require_confirmation": checked})

    def _on_voice_provider_changed(self):
        text = self.voice_combo.currentText()
        if "ElevenLabs" in text:
            provider_type = "elevenlabs"
        elif "Windows SAPI" in text:
            provider_type = "windows_sapi"
        else:
            provider_type = "disabled"
        self.voice_manager.set_provider_type(provider_type)
        self.voice_status_lbl.setText(f"Voice Status: {self.voice_manager.get_status()}")

    def _save_elevenlabs_credentials(self):
        key = self.elevenlabs_key_input.text().strip()
        voice_id = self.elevenlabs_voice_id_input.text().strip()

        try:
            if key and key != MASKED_KEY:
                set_secret("elevenlabs_api_key", key)
                self.elevenlabs_key_input.setText(MASKED_KEY)

            if voice_id:
                # Keep the Voice ID in secure storage as well; do not add a new
                # plaintext copy to config.json.
                set_secret("elevenlabs_voice_id", voice_id)
        except SecretStorageError as exc:
            show_error_message("Voice Settings Not Saved", str(exc))
            self.voice_status_lbl.setText(
                "Voice Status: secure credential storage unavailable"
            )
            return

        self.voice_manager.reload_provider()
        self.voice_status_lbl.setText(f"Voice Status: {self.voice_manager.get_status()}")
        show_info_message(
            "Voice Settings Saved",
            "ElevenLabs credentials and Voice ID were saved to secure OS storage.",
        )

    def _test_voice(self):
        if not self.voice_manager.is_available():
            show_error_message("Voice Unavailable", f"Voice engine is not available: {self.voice_manager.get_status()}")
            return
        self.voice_manager.speak("Greetings! I am S.E.N.T.I.A.L., your personal desktop AI assistant.")

    def _clear_chat_history(self):
        """Clears the chat history without touching persistent tasks or memories."""
        self.conversation_history.clear()
        self.conversation_display.clear()
        show_info_message("Chat Cleared", "Conversation history cleared. Tasks and persistent memories remain intact.")

    def _load_settings(self):
        """Loads non-sensitive config and secure secrets."""
        app_config = load_config()
        model_name = app_config.get("gemini_model", DEFAULT_MODEL)
        self.gemini_model_selector.setCurrentText(model_name)
        self.ai_provider.set_model(model_name)
        self.model_status_label.setText(f"Active Model: {model_name}")

        try:
            stored_key = self.ai_provider.get_api_key_securely()
            if stored_key:
                self.gemini_api_key_input.setText(MASKED_KEY)
                self.ai_provider.set_api_key(stored_key)
                logger.info("Gemini API key restored securely.")
                self._refresh_agent_status()
            else:
                logger.warning("No stored API key found.")
                self.ai_status_label.setText("AI Engine: Key Not Configured")
        except Exception as e:
            logger.error(f"Failed to load secure key: {e}")
            self.ai_status_label.setText("AI Engine: Security Error")

    def _current_model_name(self):
        return self.gemini_model_selector.currentText().strip() or DEFAULT_MODEL

    @asyncSlot()
    async def _save_and_test_gemini_key(self):
        """Saves and tests Gemini API Key."""
        api_key = self.gemini_api_key_input.text().strip()
        model_name = self._current_model_name()

        if api_key != MASKED_KEY:
            if not api_key:
                show_error_message("Validation Error", "API Key cannot be blank.")
                return
            try:
                self.ai_provider.save_api_key_securely(api_key)
                self.ai_provider.set_api_key(api_key)
            except Exception as e:
                show_error_message("Storage Error", f"Could not save API key: {e}")
                return

        # Save model preference to config JSON
        save_config({"gemini_model": model_name})
        self.ai_provider.set_model(model_name)
        self.model_status_label.setText(f"Active Model: {model_name}")

        self.save_key_button.setEnabled(False)
        self.ai_status_label.setText("AI Engine: Testing Connection...")
        self.ai_state_changed.emit("thinking")

        try:
            ok = await asyncio.to_thread(self.ai_provider.test_connection, model_name)
            if ok:
                show_info_message("Success", f"Successfully connected to Gemini API with {model_name}!")
                self.gemini_api_key_input.setText(MASKED_KEY)
                self._refresh_agent_status()
            else:
                show_error_message("Connection Failed", "Unable to connect with provided credentials/model.")
                self.ai_status_label.setText("AI Engine: Connection Failed")
        except Exception as e:
            show_error_message("Test Error", str(e))
            self.ai_status_label.setText("AI Engine: Test Error")
        finally:
            self.save_key_button.setEnabled(True)
            self.ai_state_changed.emit("idle")

    # ------------------------------------------------------------------ Voice Recording Slot

    @asyncSlot()
    async def _toggle_microphone_recording(self):
        """Starts or stops push-to-talk voice recording and routes transcription to the agent."""
        if self._busy:
            return

        if self.mic_service.is_recording():
            # Stop recording and transcribe
            self.mic_button.setText("🎤")
            self.mic_button.setStyleSheet("")
            self.ai_status_label.setText("AI Engine: Transcribing Audio...")
            self.ai_state_changed.emit("thinking")

            wav_bytes = self.mic_service.stop_recording()
            if not wav_bytes or len(wav_bytes) < 200:
                self.ai_status_label.setText("AI Engine: No Audio Captured")
                self.ai_state_changed.emit("idle")
                return

            try:
                transcript = await asyncio.to_thread(
                    transcribe_audio_bytes, wav_bytes, self.ai_provider
                )
                if transcript:
                    logger.info(f"Speech Recognized: {transcript}")
                    self.command_input.setText(transcript)
                    await self._send_command()
                else:
                    show_info_message("Voice Input", "No speech detected in audio recording.")
                    self._refresh_agent_status()
                    self.ai_state_changed.emit("idle")
            except Exception as e:
                show_error_message("Speech Recognition Error", str(e))
                self.ai_status_label.setText("AI Engine: Error")
                self.ai_state_changed.emit("error")
        else:
            if not self.mic_service.has_microphone():
                show_error_message("Microphone Missing", "No microphone input device detected on your system.")
                return

            started = self.mic_service.start_recording()
            if started:
                self.mic_button.setText("🔴")
                self.mic_button.setStyleSheet("background-color: rgba(255, 68, 68, 120); border: 1px solid #FF4444;")
                self.ai_status_label.setText("AI Engine: Recording Voice... Click 🔴 to Stop")
                self.ai_state_changed.emit("speaking")

    # ------------------------------------------------------------------ Chat Slot

    @asyncSlot()
    async def _send_command(self):
        """Route one user command through the agent engine or safe chat-only fallback.

        The legacy intent dispatcher is intentionally not used here: a command
        is handled by exactly one system, and the fallback is chat-only so it
        cannot execute an unverified action.
        """
        if self._busy:
            return

        user_text = self.command_input.text().strip()
        if not user_text:
            return

        if not self.ai_provider.is_configured():
            show_error_message(
                "Configuration Needed",
                "Gemini API key is not configured. Please set your key in Settings (⚙️)."
            )
            return

        prior_history = list(self.conversation_history)
        self.command_input.clear()
        self._append_message("User", user_text)

        agent_available = self._agent_available()
        self._agent_run_active = agent_available
        self._set_busy(True)
        self._idle_timer.stop()
        self.ai_state_changed.emit("thinking")

        if agent_available:
            self.ai_status_label.setText("AI Engine: Agent planning...")
            self.agent_progress_label.setText("Agent: Planning...")
        else:
            self.ai_status_label.setText("AI Engine: Chat-only fallback...")
            self.agent_progress_label.setText("Agent: Unavailable")

        try:
            if agent_available:
                try:
                    result = await self.agent_service.run(
                        user_text, conversation_history=prior_history
                    )
                except AgentServiceBusyError:
                    self._handle_gemini_error("An agent run is already active.")
                    return
                except AgentServiceUnavailable as exc:
                    # The provider may have changed between the availability
                    # check and the run.  Fall back to honest chat-only mode.
                    reason = str(exc)
                    self._agent_run_active = False
                    self._set_busy(True)
                    self.ai_status_label.setText("AI Engine: Chat-only fallback...")
                    self.agent_progress_label.setText("Agent: Unavailable")
                    await self._run_chat_only_fallback(user_text, reason)
                    return
                except AgentServiceError as exc:
                    self._handle_gemini_error(str(exc))
                    return

                self._handle_agent_result(result)
            else:
                await self._run_chat_only_fallback(
                    user_text, self._agent_unavailable_reason()
                )
        except Exception as e:
            logger.exception("Error processing AI request.")
            self._handle_gemini_error(str(e))
        finally:
            self._agent_run_active = False
            self._close_active_confirmation()
            self._set_busy(False)

    # ------------------------------------------------------------------ Agent integration

    def _agent_available(self):
        """Return True only when the real agent engine can run a workflow."""
        if self.agent_service is None:
            return False
        try:
            return bool(self.agent_service.is_available())
        except Exception:
            logger.exception("Agent availability check failed.")
            return False

    def _agent_unavailable_reason(self):
        if self.agent_service is None:
            return "The agent service is not initialized."
        try:
            return (
                self.agent_service.unavailable_reason()
                or "The agent engine is unavailable."
            )
        except Exception:
            logger.exception("Could not determine why the agent engine is unavailable.")
            return "The agent engine is unavailable."

    def _refresh_agent_status(self):
        """Update the status card without claiming the agent is ready when it is not."""
        if not self.ai_provider.is_configured():
            self.ai_status_label.setText("AI Engine: Key Not Configured")
            return
        if self._agent_available():
            self.ai_status_label.setText("AI Engine: Agent Ready")
        else:
            self.ai_status_label.setText("AI Engine: Chat-only (agent unavailable)")

    def _on_agent_event(self, event):
        """Display real agent state and tool progress from the worker thread."""
        if not self._agent_run_active:
            return

        state = getattr(event, "state", None)
        state_value = getattr(state, "value", str(state))
        kind = getattr(event, "kind", "")
        message = getattr(event, "message", "")
        tool_name = getattr(event, "tool_name", None) or "tool"
        data = getattr(event, "data", {}) or {}

        if kind == "state_changed":
            self.agent_progress_label.setText(
                f"Agent: {state_value} — {message}"[:300]
            )
            if state in {
                AgentState.PLANNING,
                AgentState.RUNNING,
                AgentState.WAITING_FOR_CONFIRMATION,
                AgentState.CANCELLING,
            }:
                self.ai_state_changed.emit("thinking")
            elif state is AgentState.COMPLETED:
                self.ai_state_changed.emit("speaking")
            elif state is AgentState.FAILED:
                self.ai_state_changed.emit("error")
            elif state is AgentState.CANCELLED:
                self.ai_state_changed.emit("idle")
        elif kind == "model_request":
            self.agent_progress_label.setText(
                f"Agent: requesting model turn {getattr(event, 'turn', '')}..."
            )
        elif kind == "tool_started":
            self.agent_progress_label.setText(
                f"Agent: running tool '{tool_name}'..."
            )
        elif kind == "tool_finished":
            success = data.get("success")
            code = data.get("code", "")
            self.agent_progress_label.setText(
                f"Agent: tool '{tool_name}' finished (success={success}, code={code})."
            )
        elif kind == "confirmation_requested":
            self.agent_progress_label.setText(
                f"Agent: waiting for your confirmation for '{tool_name}'..."
            )
        elif kind == "confirmation_denied":
            self.agent_progress_label.setText(
                f"Agent: confirmation denied for '{tool_name}'."
            )
        elif kind == "confirmation_granted":
            self.agent_progress_label.setText(
                f"Agent: confirmation granted for '{tool_name}'."
            )
        elif kind in {"failed", "cancelled", "completed"}:
            self.agent_progress_label.setText(
                f"Agent: {kind} — {message}"[:300]
            )

    def _confirmation_consequence(self, tool_name):
        consequences = {
            "create_task": (
                "This will add a new task to your persistent task list. "
                "It changes stored data and will be recorded if approved."
            ),
        }
        return consequences.get(
            tool_name,
            "This will execute a state-changing tool. Approve only if you intend "
            "this exact action.",
        )

    def _on_agent_confirmation_requested(self, prompt):
        """Show a non-modal confirmation dialog in the GUI thread."""
        request = getattr(prompt, "request", None)
        request_id = getattr(prompt, "request_id", "")
        if request is None or not request_id:
            return

        if not self._agent_run_active or self.agent_service is None:
            if self.agent_service is not None:
                self.agent_service.resolve_confirmation(request_id, False)
            return

        # A sequential agent should only have one prompt at a time.  If a stale
        # dialog is still open, deny it before presenting the new request.
        if self._active_confirmation_box is not None:
            old_id = self._active_confirmation_id
            self._close_active_confirmation()
            if old_id:
                self.agent_service.resolve_confirmation(old_id, False)

        tool_name = getattr(request, "tool_name", "unknown")
        description = getattr(request, "description", "")
        permission = getattr(getattr(request, "permission", None), "value", "CONFIRMATION_REQUIRED")
        try:
            arguments = request.to_dict().get("arguments", {})
            arguments_json = json.dumps(
                arguments, indent=2, ensure_ascii=False, default=str
            )
        except Exception:
            arguments_json = str(getattr(request, "arguments", {}))

        preview = arguments_json
        if len(preview) > 800:
            preview = preview[:797] + "..."

        box = QMessageBox(self)
        box.setWindowTitle("Confirm Agent Action")
        box.setIcon(QMessageBox.Warning)
        box.setText(
            f"S.E.N.T.I.A.L. wants to run the tool: {tool_name}\n"
            f"Permission: {permission}"
        )
        box.setInformativeText(
            f"Description: {description}\n\n"
            f"Consequence: {self._confirmation_consequence(tool_name)}\n\n"
            f"Validated arguments:\n{preview}"
        )
        if len(arguments_json) > 800:
            box.setDetailedText(arguments_json[:8000])
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        box.setWindowModality(Qt.NonModal)
        box.finished.connect(
            lambda result, rid=request_id, b=box: self._on_confirmation_dialog_finished(
                rid, result, b
            )
        )

        self._active_confirmation_box = box
        self._active_confirmation_id = request_id
        try:
            box.open()
        except Exception:
            logger.exception("Could not display the agent confirmation dialog.")
            self._active_confirmation_box = None
            self._active_confirmation_id = None
            self.agent_service.resolve_confirmation(request_id, False)
            self._append_message(
                "S.E.N.T.I.A.L. (System)",
                "The confirmation dialog could not be displayed; the action was denied.",
                store=False,
            )

    def _on_confirmation_dialog_finished(self, request_id, result, box):
        if self._active_confirmation_box is box:
            self._active_confirmation_box = None
            self._active_confirmation_id = None

        approved = result == int(QMessageBox.Yes)
        accepted = False
        if self.agent_service is not None:
            accepted = self.agent_service.resolve_confirmation(request_id, approved)
        try:
            box.deleteLater()
        except Exception:
            pass

        if not accepted:
            self._append_message(
                "S.E.N.T.I.A.L. (System)",
                "The confirmation request expired, was cancelled, or was no "
                "longer valid; no action was taken.",
                store=False,
            )

    def _close_active_confirmation(self):
        box = self._active_confirmation_box
        self._active_confirmation_box = None
        self._active_confirmation_id = None
        if box is None:
            return
        try:
            box.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        try:
            box.reject()
        except Exception:
            pass
        try:
            box.deleteLater()
        except Exception:
            pass

    def _cancel_agent_run(self):
        """Cancel the active agent run without blocking the GUI event loop."""
        if not self._agent_run_active or self.agent_service is None:
            return

        self.cancel_button.setEnabled(False)
        self.ai_status_label.setText("AI Engine: Cancelling...")
        self.agent_progress_label.setText("Agent: cancellation requested...")
        self._close_active_confirmation()

        try:
            accepted = self.agent_service.cancel("Cancelled by user")
        except Exception:
            logger.exception("Agent cancellation failed.")
            accepted = False
        if not accepted:
            self.agent_progress_label.setText("Agent: no active run to cancel")

    def _display_tool_results(self, result):
        """Show the actual structured outcomes returned by the tool registry."""
        for tool_result in getattr(result, "tool_results", []) or []:
            name = getattr(tool_result, "tool_name", "tool")
            success = bool(getattr(tool_result, "success", False))
            code = getattr(tool_result, "code", "") or "unknown"
            message = getattr(tool_result, "message", "") or ""
            error = getattr(tool_result, "error", None)
            if success:
                text = f"Tool '{name}' succeeded ({code}). {message}".strip()
            else:
                detail = error or message or "no details were returned"
                text = f"Tool '{name}' failed ({code}). {detail}".strip()
            if len(text) > 600:
                text = text[:597] + "..."
            self._append_message("S.E.N.T.I.A.L. (System)", text, store=False)

    def _handle_agent_result(self, result):
        """Render a terminal AgentRunResult honestly, including partial failures."""
        if not isinstance(result, AgentRunResult):
            self._handle_gemini_error("The agent returned an invalid result object.")
            return

        self._display_tool_results(result)

        if result.state is AgentState.COMPLETED:
            final_text = (result.final_text or "").strip() or result.message
            if not final_text:
                final_text = "The agent completed without a text response."

            if result.failed_tool_results:
                # The model can still produce a confident summary after a tool
                # failure.  Never present that as confirmed success.
                self._append_message(
                    "S.E.N.T.I.A.L.",
                    "Unverified agent summary (one or more tool calls failed):\n"
                    + final_text,
                )
                self._append_message(
                    "S.E.N.T.I.A.L. (System)",
                    "Warning: one or more tools failed. No success is confirmed "
                    "for those actions; check the tool results above.",
                    store=False,
                )
                self.agent_progress_label.setText(
                    "Agent: Completed with tool failures"
                )
                activity_text = "Agent completed with one or more tool failures."
                speech_text = (
                    "One or more agent actions failed. No success was confirmed."
                )
            else:
                self._append_message("S.E.N.T.I.A.L.", final_text)
                self.agent_progress_label.setText("Agent: Completed")
                activity_text = f"Agent responded: {final_text[:60]}..."
                speech_text = final_text

            self.ai_state_changed.emit("speaking")
            self.ai_status_label.setText("AI Engine: Agent Ready")
            try:
                self.dashboard_view.append_activity(activity_text)
            except Exception:
                logger.debug("Dashboard activity update failed.", exc_info=True)
            self._speak_response(speech_text)
            self._idle_timer.start(2500)
        elif result.state is AgentState.CANCELLED:
            self._append_message(
                "S.E.N.T.I.A.L. (System)",
                "Agent run cancelled. No further tool calls were scheduled. Any "
                "tool that was already running may have been abandoned; verify "
                "its external effect before retrying.",
                store=False,
            )
            self.ai_state_changed.emit("idle")
            self.ai_status_label.setText("AI Engine: Agent Cancelled")
            self.agent_progress_label.setText("Agent: Cancelled")
            self._idle_timer.start(2500)
        else:
            detail = result.error or result.message or "The agent workflow failed."
            self._append_message(
                "S.E.N.T.I.A.L. (System)",
                f"Agent run failed [{result.error_code or 'failed'}]: {detail}",
                store=False,
            )
            self.ai_state_changed.emit("error")
            self.ai_status_label.setText("AI Engine: Agent Error")
            self.agent_progress_label.setText(
                f"Agent: Failed ({result.error_code or 'failed'})"
            )
            self._idle_timer.start(4000)

    async def _run_chat_only_fallback(self, user_text, reason):
        """Use the existing Gemini chat transport for conversation only.

        This path deliberately cannot execute actions: it never calls the
        intent dispatcher, automation, browser, Gmail, WhatsApp, task or memory
        services.  The system context explicitly tells the model not to claim
        that any action happened.
        """
        if not self._agent_fallback_notice_shown:
            self._append_message(
                "S.E.N.T.I.A.L. (System)",
                f"Agent action engine unavailable: {reason} Chat-only mode is "
                "active; no actions can be executed.",
                store=False,
            )
            self._agent_fallback_notice_shown = True

        self.agent_progress_label.setText("Agent: Chat-only fallback")
        self.ai_status_label.setText("AI Engine: Chat-only fallback")

        history = self._build_gemini_history(limit=20)
        system_context = self._build_fallback_context(user_text)
        response_text = await asyncio.to_thread(
            self.ai_provider.send_message,
            history,
            self._current_model_name(),
            system_context,
        )
        response_text = str(response_text or "").strip()
        if not response_text:
            raise RuntimeError("The chat-only fallback returned an empty response.")
        self._handle_gemini_response(response_text)
        self.ai_status_label.setText("AI Engine: Chat-only fallback")
        self.agent_progress_label.setText("Agent: Chat-only fallback")
        self._speak_response(response_text)

    def _build_fallback_context(self, user_text):
        parts = []
        try:
            memory_context = self.memory_service.build_context_for_prompt(user_text)
            if memory_context:
                parts.append("[USER CONTEXT & MEMORIES]\n" + str(memory_context)[:6000])
        except Exception:
            logger.warning("Memory context could not be built for the fallback.")

        parts.append(
            "IMPORTANT: The agent action engine is unavailable. You are in "
            "chat-only mode. You cannot create tasks, launch applications, browse, "
            "send messages, or modify any data. Do not claim that any action was "
            "performed. If the user asks for an action, explain that the action "
            "engine is unavailable and that no action was taken."
        )
        return "\n\n".join(parts)

    def _speak_response(self, text):
        if not text:
            return
        try:
            if self.voice_manager.is_available():
                self.voice_manager.speak(text)
        except Exception:
            logger.warning("Voice playback failed; the response remains visible.")

    def _build_gemini_history(self, limit=20):
        """Converts a bounded conversation history to Gemini list format."""
        messages = self.conversation_history[-limit:] if limit else self.conversation_history
        return [
            {"role": "user" if role == "User" else "model", "parts": [text]}
            for role, text in messages
            if text
        ]

    def closeEvent(self, event):
        """Stop the agent worker and deny any pending prompt before closing."""
        try:
            self._close_active_confirmation()
        finally:
            if self.agent_service is not None:
                try:
                    self.agent_service.shutdown()
                except Exception:
                    logger.exception("Agent service shutdown failed.")
            super().closeEvent(event)

    def _set_busy(self, busy):
        self._busy = busy
        self.send_button.setEnabled(not busy)
        self.command_input.setEnabled(not busy)
        self.mic_button.setEnabled(not busy)
        self.cancel_button.setEnabled(bool(busy and self._agent_run_active))
        if not busy:
            self.command_input.setFocus()

    def _handle_gemini_response(self, response_text):
        self._append_message("S.E.N.T.I.A.L.", response_text)
        self.ai_state_changed.emit("speaking")
        self.ai_status_label.setText("AI Engine: Online & Ready")
        if hasattr(self, "dashboard_view"):
            self.dashboard_view.append_activity(f"S.E.N.T.I.A.L. Responded: {response_text[:60]}...")
        self._idle_timer.start(2500)

    def _handle_gemini_error(self, error_msg):
        if self.conversation_history and self.conversation_history[-1][0] == "User":
            self.conversation_history.pop()

        self._append_message("S.E.N.T.I.A.L. (System)", f"Error: {error_msg}", store=False)
        self.ai_state_changed.emit("error")
        self.ai_status_label.setText("AI Engine: Error")
        self._idle_timer.start(4000)

    def _append_message(self, sender, text, store=True):
        if store:
            self.conversation_history.append((sender, text))
            if len(self.conversation_history) > MAX_CONVERSATION_MESSAGES:
                del self.conversation_history[:-MAX_CONVERSATION_MESSAGES]

        color_map = {
            "User": "#05D9E8",
            "S.E.N.T.I.A.L.": "#FF2A6D",
            "S.E.N.T.I.A.L. (System)": "#9D4EDD"
        }
        color = color_map.get(sender, "#E0E0E0")
        safe_text = html.escape(str(text)).replace("\n", "<br>")

        formatted_msg = (
            f'<div style="margin-bottom: 12px;">'
            f'<b style="color: {color}; font-size: 11pt;">{html.escape(sender)}:</b><br>'
            f'<span style="color: #F0F4F8; font-size: 10pt; line-height: 1.4;">{safe_text}</span>'
            f'</div>'
        )

        self.conversation_display.append(formatted_msg)
        bar = self.conversation_display.verticalScrollBar()
        bar.setValue(bar.maximum())
