"""
Action items: CRUD, resolve/unresolve, archive/unarchive.
"""

import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException

from app.config import OPENCLAW_ENABLED
from app.database import get_db, get_db_write
from app.models import ActionItemCreate
from app.websocket import manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/tasks/{task_id}/action-items")
def get_action_items(task_id: int, resolved: bool = False, archived: bool = False):
    """Get action items for a task."""
    with get_db() as conn:
        if archived:
            rows = conn.execute(
                "SELECT * FROM action_items WHERE task_id = ? AND archived = 1 ORDER BY created_at ASC",
                (task_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM action_items WHERE task_id = ? AND resolved = ? AND archived = 0 ORDER BY created_at ASC",
                (task_id, 1 if resolved else 0)
            ).fetchall()
        return [dict(row) for row in rows]


@router.post("/api/tasks/{task_id}/action-items")
async def add_action_item(task_id: int, item: ActionItemCreate):
    """Add an action item to a task."""
    now = datetime.now().isoformat()
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        cursor = conn.execute(
            "INSERT INTO action_items (task_id, comment_id, agent, content, item_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, item.comment_id, item.agent, item.content, item.item_type, now)
        )
        result = {
            "id": cursor.lastrowid, "task_id": task_id, "comment_id": item.comment_id,
            "agent": item.agent, "content": item.content, "item_type": item.item_type,
            "resolved": 0, "created_at": now, "resolved_at": None
        }
    logger.info(f"Action item added on task #{task_id} by {item.agent}: {item.content[:50]}")
    await manager.broadcast({"type": "action_item_added", "task_id": task_id, "item": result})
    return result


@router.post("/api/action-items/{item_id}/resolve")
async def resolve_action_item(item_id: int):
    """Resolve an action item."""
    now = datetime.now().isoformat()
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM action_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action item not found")
        conn.execute("UPDATE action_items SET resolved = 1, resolved_at = ? WHERE id = ?", (now, item_id))
        task_id = row["task_id"]
    logger.info(f"Action item #{item_id} resolved on task #{task_id}")
    await manager.broadcast({"type": "action_item_resolved", "task_id": task_id, "item_id": item_id})

    # Notify the running agent about the resolved action item
    if OPENCLAW_ENABLED:
        with get_db() as conn:
            task = conn.execute("SELECT agent_session_key, title FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task and task["agent_session_key"]:
            from app.openclaw import send_to_agent_session
            item_type = row["item_type"]
            content = row["content"]
            msg = f"✅ Action item resolved by supervisor on task \"{task['title']}\":\n[{item_type}] {content}"
            sent = await send_to_agent_session(task["agent_session_key"], msg)
            if sent:
                logger.info(f"Notified agent about resolved action item #{item_id}")

    return {"success": True, "item_id": item_id}


@router.post("/api/action-items/{item_id}/unresolve")
async def unresolve_action_item(item_id: int):
    """Unresolve an action item."""
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM action_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action item not found")
        conn.execute("UPDATE action_items SET resolved = 0, resolved_at = NULL WHERE id = ?", (item_id,))
        task_id = row["task_id"]
    logger.info(f"Action item #{item_id} unresolved on task #{task_id}")
    await manager.broadcast({"type": "action_item_unresolved", "task_id": task_id, "item_id": item_id})
    return {"success": True, "item_id": item_id}


@router.post("/api/action-items/{item_id}/archive")
async def archive_action_item(item_id: int):
    """Archive a resolved action item."""
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM action_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action item not found")
        conn.execute("UPDATE action_items SET archived = 1 WHERE id = ?", (item_id,))
        task_id = row["task_id"]
    logger.info(f"Action item #{item_id} archived on task #{task_id}")
    await manager.broadcast({"type": "action_item_archived", "task_id": task_id, "item_id": item_id})
    return {"success": True, "item_id": item_id}


@router.post("/api/action-items/{item_id}/unarchive")
async def unarchive_action_item(item_id: int):
    """Unarchive an action item."""
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM action_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action item not found")
        conn.execute("UPDATE action_items SET archived = 0 WHERE id = ?", (item_id,))
        task_id = row["task_id"]
    logger.info(f"Action item #{item_id} unarchived on task #{task_id}")
    await manager.broadcast({"type": "action_item_unarchived", "task_id": task_id, "item_id": item_id})
    return {"success": True, "item_id": item_id}


@router.delete("/api/action-items/{item_id}")
async def delete_action_item(item_id: int):
    """Delete an action item."""
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM action_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action item not found")
        task_id = row["task_id"]
        conn.execute("DELETE FROM action_items WHERE id = ?", (item_id,))
    logger.info(f"Action item #{item_id} deleted from task #{task_id}")
    await manager.broadcast({"type": "action_item_deleted", "task_id": task_id, "item_id": item_id})
    return {"success": True, "item_id": item_id}
