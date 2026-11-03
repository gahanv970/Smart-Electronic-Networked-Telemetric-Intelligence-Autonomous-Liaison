# S.E.N.T.I.A.L. — Personal Windows Desktop AI Assistant

**S.E.N.T.I.A.L.** (*Smart Electronic Networked Telemetric Intelligence & Autonomous Liaison*) is a cloud-powered personal desktop AI assistant built natively with **Python**, **PySide6**, and the modern official **Google Gen AI SDK** (`google-genai`). Featuring an anime-inspired cyberpunk interface and animated AI core visualization, S.E.N.T.I.A.L. provides intelligent chat, persistent task management, long-term memory personalization, safe Windows/browser automation, Gmail draft & email controls, WhatsApp companion mode, and a centralized Notification Intelligence Alert Center.

---

## ✨ Key Capabilities

- **Anime Cyberpunk Interface**: Original futuristic anime desktop interface with deep midnight background, glowing crimson/cyan/violet neon accents, custom navigation, and a Home Dashboard.
- **Home Dashboard**: Polished dashboard featuring system status, live digital clock, Gemini AI and ElevenLabs connection indicators, quick action tiles, notification summaries, and recent activity logs.
- **Animated S.E.N.T.I.A.L. Core**: Dynamic AI core visualization reflecting real-time AI states (*Idle*, *Thinking*, *Speaking*, *Error*).
- **Google Gen AI SDK (`google-genai`)**: Centralized Gemini AI integration with `gemini-3.8-flash` as the default model and structured intent understanding with strict allowlist validation.
- **ElevenLabs & SAPI Voice**: Official ElevenLabs REST API text-to-speech voice clone provider with seamless fallback to native Windows SAPI.
- **Notification Intelligence & Alert Center**: Centralized alert aggregation feed ranking WhatsApp Desktop and Gmail notifications by urgency (`CRITICAL`, `HIGH`, `NORMAL`, `LOW`) with priority contact locks against prompt injection.
- **WhatsApp Personal Companion**: Smart contact intelligence, message analysis, urgency rating, AI reply drafting, and clipboard copying with optional Windows WinRT notification monitoring.
- **Persistent Task Management**: Full SQLite-backed task tracking with CRUD operations, priority levels (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`), due dates, status toggles, filtering, and real-time search.
- **Memory & Personalization Service**: Persistent context store and user preferences (name, communication tone, programming preferences). Injects relevant memories into Gemini prompts; automatically rejects sensitive credentials or secrets.
- **Safe Windows & Browser Automation**: Controlled action allowlist for approved apps (Notepad, Calculator, File Explorer, Paint, Task Manager, Command Prompt), validated HTTP/HTTPS URL opening, private network blocking, path traversal protection, readable web page text extraction, and multi-step workflow orchestration. Consequential actions require explicit user confirmation.
- **Chat Website Opening**: Ask S.E.N.T.I.A.L. to open a website such as YouTube; the requested URL is validated and requires confirmation before the default browser is launched.
- **Live News Search**: Ask for current news or events to retrieve recent headlines from Google News RSS, with publisher, publication time, and source links.
- **Google Gmail Integration**: Official Google Gmail API integration using OAuth 2.0 InstalledAppFlow, mailbox search, message detail viewing, draft sync, and strictly user-confirmed email dispatching.
- **Secure Secret Storage**: API keys and OAuth tokens stored securely via the OS Keyring / Credential Manager with encrypted local fallbacks.

---

## 🛠️ Project Architecture

```
S.E.N.T.I.A.L./
├── main.py                  # Application entry point with qasync event loop integration
├── Jarvis_UI.py             # Main window controller, navigation, and chat handling
├── ui_views.py              # Dedicated UI widgets for Dashboard, Tasks, Memory, Email, WhatsApp, Alerts, Workflows, Automation
├── gemini_api.py            # Centralized Google Gen AI provider with structured intent parsing
├── task_service.py          # SQLite-backed persistent task manager (CRUD, filtering, search)
├── memory_service.py        # Persistent context & personalization service with secret filtering
├── gmail_service.py         # Official Gmail API service (OAuth 2.0, mailbox, drafts, safe send)
├── whatsapp_service.py      # Smart contact intelligence, WinRT notification listener, AI message analysis, and reply drafting
├── alert_service.py         # Centralized Alert Notification aggregation service
├── workflow_engine.py       # Multi-step sequential workflow orchestration engine
├── browser_service.py       # Browser automation, URL scheme validation, private IP blocking, and HTML text extraction
├── automation_service.py    # Windows automation engine (allowlist, path traversal protection, system telemetry)
├── voice_service.py         # ElevenLabs REST TTS provider with Windows SAPI fallback
├── microphone_service.py    # PySide6 QtMultimedia push-to-talk microphone STT service
├── ai_core_visualization.py # Animated S.E.N.T.I.A.L. Core widget
├── gui_components.py        # Anime Cyberpunk stylesheet and glass card components
├── config.py                # Configuration and secure credential manager (Keyring + fallback)
├── utils.py                 # Application-wide logging and message dialogs
├── requirements.txt         # Project dependencies
├── tests/                   # Comprehensive automated test suite (111 unit tests)
└── data/                    # Local persistent storage (.gitignore protected)
```

---

## 🚀 Getting Started

### Prerequisites

- **Windows 10 / 11**
- **Python 3.11+**
- **Google Gemini API Key**: Obtain a key from [Google AI Studio](https://aistudio.google.com/).

### Installation

1. **Clone or Open the Repository**:
   ```bash
   cd C:\Users\welcome\.gemini\antigravity\scratch\G-AI_ASSISTANT
   ```

2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Launch Application**:
   ```bash
   python main.py
   ```

---

## ⚙️ Configuration & Setup

1. **Gemini API Key**:
   - Open S.E.N.T.I.A.L. and click **Settings** (⚙️) on the sidebar.
   - Enter your Gemini API key and select your preferred model (`gemini-3.8-flash`).
   - Click **Save & Validate Credentials**. Your key will be securely saved into OS Keyring.

2. **Gmail OAuth Integration**:
   - In Google Cloud Console, enable the **Gmail API** and create **OAuth 2.0 Client ID (Desktop app)** credentials.
   - Download the client secrets file and save it as:
     ```
     data/gmail_credentials.json
     ```
   - Navigate to the **Email** tab (📧) and click **Connect Account** to authorize via browser.

3. **Windows & Browser Automation**:
   - Applications are executed from the strictly controlled allowlist without shell interpreters.
   - You can toggle confirmation requirements under **Settings** > **Automation Security**.

4. **Running Tests**:
   ```bash
   QT_QPA_PLATFORM=offscreen python -m pytest tests -v
   ```

---

## 📄 License

Distributed under the MIT License.
