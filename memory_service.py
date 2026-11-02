"""
Memory and Personalization Service for G-AI-ASSISTANT.
Manages long-term user preferences, persistent contextual memories,
secret redaction/filtering, and context generation for Gemini AI prompt injection.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Generator, List, Optional

from config import APP_DIR, get_config_value, save_config

logger = logging.getLogger(__name__)

DB_PATH = APP_DIR / "memory.db"
PREFS_FILE = APP_DIR / "user_preferences.json"

# Patterns or keywords indicating secrets that must NEVER be stored as memories
SENSITIVE_PATTERNS = [
    re.compile(r"(AIzaSy[0-9A-Za-z\-_]{33})", re.IGNORECASE),  # Google API key
    re.compile(r"(sk-[a-zA-Z0-9]{20,})", re.IGNORECASE),       # OpenAI-style key
    re.compile(r"(ghp_[0-9A-Za-z]{36})", re.IGNORECASE),       # GitHub token
    re.compile(r"(bearer\s+[a-zA-Z0-9_\-\.]{20,})", re.IGNORECASE),
    re.compile(r"\b(password|passwd|pwd)\b\s*([:=]|is)\s*\S+", re.IGNORECASE),
    re.compile(r"\b(api[\s_-]?key|secret[\s_-]?key|client[\s_-]?secret|private[\s_-]?key|auth[\s_-]?token)\b\s*([:=]|is)\s*\S+", re.IGNORECASE),
]

SENSITIVE_KEYWORDS = [
    "password", "secret_key", "client_secret", "private_key", "auth_token", "access_token"
]


@dataclass
class MemoryEntry:
    id: Optional[int]
    category: str      # e.g., "fact", "preference", "project", "instruction"
    content: str
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MemoryService:
    """
    Dedicated service for managing user memories and preferences.
    Uses SQLite for memories and JSON for direct user preferences.
    Guarantees secrets are never accepted as memory.
    """

    def __init__(self, db_path: Optional[Path] = None, prefs_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.prefs_path = prefs_path or PREFS_FILE
        self._ensure_storage()

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for SQLite connections with clean close."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_storage(self) -> None:
        """Initializes database schema and preferences storage."""
        with self._db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL DEFAULT 'fact',
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
        if not self.prefs_path.exists():
            try:
                self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
                default_prefs = {
                    "user_name": "",
                    "preferred_tone": "Polite, helpful, and concise",
                    "code_language_preference": "Python",
                }
                with open(self.prefs_path, "w", encoding="utf-8") as f:
                    json.dump(default_prefs, f, indent=4)
            except Exception as e:
                logger.error(f"Error creating default preferences file: {e}")

    # ------------------------------------------------------------------ Memory Flags & Configuration

    def is_memory_enabled(self) -> bool:
        """Returns True if memory usage is enabled in application settings."""
        return bool(get_config_value("memory_enabled", True))

    def set_memory_enabled(self, enabled: bool) -> None:
        """Enables or disables memory usage."""
        save_config({"memory_enabled": bool(enabled)})
        logger.info(f"Memory enabled setting set to: {enabled}")

    # ------------------------------------------------------------------ Memory CRUD

    def validate_content_safety(self, text: str) -> None:
        """
        Validates that text does not contain passwords, API keys, or tokens.
        Raises ValueError if sensitive secrets are found.
        """
        if not text or not text.strip():
            raise ValueError("Memory content cannot be empty.")

        for pat in SENSITIVE_PATTERNS:
            if pat.search(text):
                raise ValueError("Security violation: Memory content appears to contain an API key, token, or password.")

        for kw in SENSITIVE_KEYWORDS:
            # Check for direct key=val pattern with keywords
            if re.search(rf"\b{kw}\b\s*[:=]\s*\S+", text, re.IGNORECASE):
                raise ValueError(f"Security violation: Storing secrets or passwords ('{kw}') is prohibited.")

    def add_memory(self, content: str, category: str = "fact") -> MemoryEntry:
        """
        Adds a new validated memory.
        """
        clean_content = (content or "").strip()
        self.validate_content_safety(clean_content)

        clean_category = (category or "fact").strip().lower()
        now = datetime.now().isoformat()

        with self._db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO memories (category, content, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (clean_category, clean_content, now, now),
            )
            memory_id = cursor.lastrowid

        logger.info(f"Added memory id={memory_id} category='{clean_category}'")
        return MemoryEntry(
            id=memory_id,
            category=clean_category,
            content=clean_content,
            created_at=now,
            updated_at=now,
        )

    def get_memory(self, memory_id: int) -> Optional[MemoryEntry]:
        """Retrieves a single memory entry."""
        with self._db() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row:
                return self._row_to_memory(row)
        return None

    def get_all_memories(self, category: Optional[str] = None, search_query: Optional[str] = None) -> List[MemoryEntry]:
        """
        Retrieves all memories, with optional filtering and search.
        """
        query = "SELECT * FROM memories WHERE 1=1"
        params: List[Any] = []

        if category and category.strip():
            query += " AND category = ?"
            params.append(category.strip().lower())

        if search_query and search_query.strip():
            query += " AND content LIKE ?"
            params.append(f"%{search_query.strip()}%")

        query += " ORDER BY id DESC"

        with self._db() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_memory(r) for r in rows]

    def update_memory(self, memory_id: int, content: str, category: Optional[str] = None) -> Optional[MemoryEntry]:
        """Updates an existing memory entry."""
        existing = self.get_memory(memory_id)
        if not existing:
            return None

        clean_content = (content or "").strip()
        self.validate_content_safety(clean_content)

        new_category = existing.category if category is None else category.strip().lower()
        now = datetime.now().isoformat()

        with self._db() as conn:
            conn.execute(
                """
                UPDATE memories
                SET content = ?, category = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean_content, new_category, now, memory_id),
            )

        logger.info(f"Updated memory id={memory_id}")
        return self.get_memory(memory_id)

    def delete_memory(self, memory_id: int) -> bool:
        """Deletes a specific memory entry."""
        with self._db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Deleted memory id={memory_id}")
            return deleted

    def clear_all_memories(self) -> int:
        """Deletes all stored memories. Returns number of deleted items."""
        with self._db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM memories")
            count = cursor.rowcount
            logger.info(f"Cleared all memories ({count} items removed).")
            return count

    # ------------------------------------------------------------------ User Preferences

    def get_preferences(self) -> Dict[str, Any]:
        """Loads user preferences from disk."""
        if not self.prefs_path.exists():
            return {}
        try:
            with open(self.prefs_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading preferences: {e}")
            return {}

    def save_preferences(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Saves user preference updates."""
        prefs = self.get_preferences()
        prefs.update(updates)
        try:
            with open(self.prefs_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f, indent=4)
            logger.info("User preferences updated.")
        except Exception as e:
            logger.error(f"Error saving preferences: {e}")
        return prefs

    # ------------------------------------------------------------------ Context Builder for Gemini

    def build_context_for_prompt(self, query: Optional[str] = None) -> Optional[str]:
        """
        Builds a combined context string containing user preferences and relevant memories.
        Returns None if memory is disabled or no context is available.
        """
        if not self.is_memory_enabled():
            logger.debug("Memory is disabled; context injection skipped.")
            return None

        parts = []

        # 1. User preferences
        prefs = self.get_preferences()
        pref_lines = []
        if prefs.get("user_name"):
            pref_lines.append(f"- User Name: {prefs['user_name']}")
        if prefs.get("preferred_tone"):
            pref_lines.append(f"- Communication Tone: {prefs['preferred_tone']}")
        if prefs.get("code_language_preference"):
            pref_lines.append(f"- Primary Programming Language: {prefs['code_language_preference']}")

        for k, v in prefs.items():
            if k not in ["user_name", "preferred_tone", "code_language_preference"] and v:
                pref_lines.append(f"- {k.replace('_', ' ').capitalize()}: {v}")

        if pref_lines:
            parts.append("User Preferences:\n" + "\n".join(pref_lines))

        # 2. Approved Memories
        memories = self.get_all_memories()
        if memories:
            # If query provided, prioritize matching memories, else top 15 most recent
            selected = memories[:15]
            mem_lines = [f"- [{m.category.upper()}] {m.content}" for m in selected]
            parts.append("Remembered Context & Facts:\n" + "\n".join(mem_lines))

        if not parts:
            return None

        return "\n\n".join(parts)

    def _row_to_memory(self, row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            id=row["id"],
            category=row["category"],
            content=row["content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
