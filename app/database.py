"""
Database: connection manager, schema initialization, activity logging.

Concurrency model:
- SQLite WAL mode for concurrent reads
- BEGIN IMMEDIATE for write transactions (serializes writes)
- busy_timeout=30s to wait for locks instead of failing
- All writes go through get_db_write() which acquires IMMEDIATE lock
- Reads can use get_db() (no lock needed with WAL)
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from app.config import DB_PATH

logger = logging.getLogger(__name__)

# Global write lock to serialize all database writes at the application level.
# This prevents SQLite BUSY errors when multiple async handlers try to write concurrently.
_write_lock = threading.Lock()


@contextmanager
def get_db():
    """Read-only database connection. Use get_db_write() for writes."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_db_write():
    """
    Write-safe database connection with serialization.

    Acquires a thread lock + BEGIN IMMEDIATE to ensure:
    1. Only one write transaction at a time (app-level lock)
    2. SQLite write lock acquired immediately (not deferred)
    3. Auto-commit on success, auto-rollback on exception
    """
    with _write_lock:
        conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()


def init_db():
    """Initialize the database."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'Backlog',
                priority TEXT DEFAULT 'Medium',
                agent TEXT DEFAULT 'Unassigned',
                due_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                board TEXT DEFAULT 'tasks'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                action TEXT NOT NULL,
                agent TEXT,
                details TEXT,
                timestamp TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                agent TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS action_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                comment_id INTEGER,
                agent TEXT NOT NULL,
                content TEXT NOT NULL,
                item_type TEXT DEFAULT 'question',
                resolved INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            )
        """)
        # Add columns if they don't exist
        for alter in [
            "ALTER TABLE tasks ADD COLUMN working_agent TEXT DEFAULT NULL",
            "ALTER TABLE tasks ADD COLUMN agent_session_key TEXT DEFAULT NULL",
            "ALTER TABLE action_items ADD COLUMN archived INTEGER DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN source_file TEXT DEFAULT NULL",
            "ALTER TABLE tasks ADD COLUMN source_ref TEXT DEFAULT NULL",
        ]:
            try:
                conn.execute(alter)
            except Exception:
                pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT DEFAULT 'main',
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                attachments TEXT,
                created_at TEXT NOT NULL
            )
        """)
        try:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN session_key TEXT DEFAULT 'main'")
        except Exception:
            pass

        # Drop legacy soft-delete table (sessions now deleted via WS RPC)
        conn.execute("DROP TABLE IF EXISTS deleted_sessions")

        # Projects table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                color TEXT DEFAULT '#00b4d8',
                created_at TEXT NOT NULL
            )
        """)

        # Add project_id to tasks if not exists
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN project_id INTEGER DEFAULT 1 REFERENCES projects(id)")
        except Exception:
            pass  # Column already exists

        # Ensure default project exists
        cursor = conn.execute("SELECT id FROM projects WHERE slug = 'default'")
        if not cursor.fetchone():
            from datetime import datetime as dt, timezone
            conn.execute(
                "INSERT INTO projects (name, slug, description, color, created_at) VALUES (?, ?, ?, ?, ?)",
                ("Default", "default", "Default project", "#00b4d8", dt.now(timezone.utc).isoformat())
            )

        conn.commit()


def log_activity(task_id: int, action: str, agent: str = None, details: str = None):
    """Log an activity with full audit trail."""
    with get_db_write() as conn:
        conn.execute(
            "INSERT INTO activity_log (task_id, action, agent, details, timestamp) VALUES (?, ?, ?, ?, ?)",
            (task_id, action, agent, details, datetime.now().isoformat())
        )
    logger.info(f"ACTIVITY: task={task_id} action={action} agent={agent} details={details}")
