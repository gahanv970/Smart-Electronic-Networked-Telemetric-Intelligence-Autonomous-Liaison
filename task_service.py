"""
Task Management Service for G-AI-ASSISTANT.
Provides persistent SQLite-backed task tracking with CRUD operations,
priority management, due date handling, status toggles, filtering, and search.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

from config import APP_DIR

DB_PATH = APP_DIR / "tasks.db"

VALID_PRIORITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
VALID_STATUSES = ["pending", "completed"]


@dataclass
class Task:
    id: Optional[int]
    title: str
    description: str
    priority: str  # "LOW", "MEDIUM", "HIGH", "CRITICAL"
    status: str    # "pending", "completed"
    due_date: Optional[str]  # "YYYY-MM-DD" or None
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TaskService:
    """
    Dedicated, thread-safe service layer for managing tasks stored in SQLite.
    Properly closes SQLite connections for Windows file handle safety.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self._ensure_db()

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that provides a SQLite connection and closes it cleanly."""
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

    def _ensure_db(self) -> None:
        """Initializes the database schema if not present."""
        with self._db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    priority TEXT NOT NULL DEFAULT 'MEDIUM',
                    status TEXT NOT NULL DEFAULT 'pending',
                    due_date TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            logger.debug(f"Tasks database verified at {self.db_path}")

    def create_task(
        self,
        title: str,
        description: str = "",
        priority: str = "MEDIUM",
        due_date: Optional[str] = None,
    ) -> Task:
        """
        Creates a new task.
        Raises ValueError if title is empty or priority is invalid.
        """
        clean_title = (title or "").strip()
        if not clean_title:
            raise ValueError("Task title cannot be empty.")

        clean_priority = (priority or "MEDIUM").upper().strip()
        if clean_priority not in VALID_PRIORITIES:
            raise ValueError(f"Invalid priority '{priority}'. Must be one of {VALID_PRIORITIES}.")

        clean_description = (description or "").strip()
        clean_due_date = due_date.strip() if due_date and due_date.strip() else None

        now = datetime.now().isoformat()
        with self._db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO tasks (title, description, priority, status, due_date, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (clean_title, clean_description, clean_priority, clean_due_date, now, now),
            )
            task_id = cursor.lastrowid

        logger.info(f"Created task id={task_id} title='{clean_title}' priority='{clean_priority}'")
        return Task(
            id=task_id,
            title=clean_title,
            description=clean_description,
            priority=clean_priority,
            status="pending",
            due_date=clean_due_date,
            created_at=now,
            updated_at=now,
        )

    def get_task(self, task_id: int) -> Optional[Task]:
        """Retrieves a single task by ID."""
        with self._db() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row:
                return self._row_to_task(row)
        return None

    def get_all_tasks(
        self,
        status_filter: Optional[str] = None,
        priority_filter: Optional[str] = None,
        search_query: Optional[str] = None,
        sort_by: str = "created_desc",
    ) -> List[Task]:
        """
        Retrieves tasks with optional filtering, search, and sorting.
        """
        query = "SELECT * FROM tasks WHERE 1=1"
        params: List[Any] = []

        if status_filter and status_filter.lower() in ["pending", "completed"]:
            query += " AND status = ?"
            params.append(status_filter.lower())

        if priority_filter and priority_filter.upper() in VALID_PRIORITIES:
            query += " AND priority = ?"
            params.append(priority_filter.upper())

        if search_query and search_query.strip():
            query += " AND (title LIKE ? OR description LIKE ?)"
            pattern = f"%{search_query.strip()}%"
            params.extend([pattern, pattern])

        if sort_by == "created_asc":
            query += " ORDER BY id ASC"
        elif sort_by == "due_date":
            query += " ORDER BY CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END, due_date ASC, id DESC"
        elif sort_by == "priority":
            query += """
                ORDER BY CASE priority
                    WHEN 'CRITICAL' THEN 1
                    WHEN 'HIGH' THEN 2
                    WHEN 'MEDIUM' THEN 3
                    WHEN 'LOW' THEN 4
                    ELSE 5
                END, id DESC
            """
        else:  # default 'created_desc'
            query += " ORDER BY id DESC"

        with self._db() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_task(r) for r in rows]

    def update_task(
        self,
        task_id: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[str] = None,
        due_date: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[Task]:
        """Updates an existing task."""
        existing = self.get_task(task_id)
        if not existing:
            return None

        new_title = existing.title if title is None else title.strip()
        if not new_title:
            raise ValueError("Task title cannot be empty.")

        new_desc = existing.description if description is None else description.strip()

        new_priority = existing.priority
        if priority is not None:
            clean_pri = priority.upper().strip()
            if clean_pri not in VALID_PRIORITIES:
                raise ValueError(f"Invalid priority '{priority}'.")
            new_priority = clean_pri

        new_due_date = existing.due_date if due_date is None else (due_date.strip() or None)

        new_status = existing.status
        if status is not None:
            clean_status = status.lower().strip()
            if clean_status not in VALID_STATUSES:
                raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}.")
            new_status = clean_status

        now = datetime.now().isoformat()
        with self._db() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET title = ?, description = ?, priority = ?, status = ?, due_date = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_title, new_desc, new_priority, new_status, new_due_date, now, task_id),
            )

        logger.info(f"Updated task id={task_id}")
        return self.get_task(task_id)

    def toggle_task_status(self, task_id: int) -> Optional[Task]:
        """Toggles a task between 'pending' and 'completed'."""
        task = self.get_task(task_id)
        if not task:
            return None
        new_status = "completed" if task.status == "pending" else "pending"
        return self.update_task(task_id, status=new_status)

    def delete_task(self, task_id: int) -> bool:
        """Deletes a task by ID."""
        with self._db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Deleted task id={task_id}")
            return deleted

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        """Converts a SQLite Row into a Task instance."""
        return Task(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            priority=row["priority"],
            status=row["status"],
            due_date=row["due_date"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
