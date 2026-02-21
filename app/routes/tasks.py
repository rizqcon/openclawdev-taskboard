"""
Task CRUD, move, start-work, stop-work, agent tasks, config, activity.
"""

import logging
from typing import List
from datetime import datetime
from fastapi import APIRouter, HTTPException

import app.config as cfg
from app.config import (
    STATUSES, PRIORITIES,
    MAIN_AGENT_NAME, MAIN_AGENT_EMOJI, HUMAN_NAME, HUMAN_SUPERVISOR_LABEL, BOARD_TITLE,
    AUTO_STOP_ON_DONE,
)
from app.database import get_db, get_db_write, log_activity
from app.models import TaskCreate, TaskUpdate, Task
from app.websocket import manager
from app.openclaw import (
    spawn_agent_session, send_to_agent_session,
    get_task_session, set_task_session,
    is_session_alive, stop_agent_session,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/config")
def get_config():
    """Get board configuration including branding."""
    with get_db() as conn:
        projects = [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]
    return {
        "agents": cfg.AGENTS,
        "agentMeta": cfg.AGENT_META,
        "statuses": STATUSES,
        "priorities": PRIORITIES,
        "projects": projects,
        "branding": {
            "mainAgentName": MAIN_AGENT_NAME,
            "mainAgentEmoji": MAIN_AGENT_EMOJI,
            "humanName": HUMAN_NAME,
            "humanSupervisorLabel": HUMAN_SUPERVISOR_LABEL,
            "boardTitle": BOARD_TITLE,
        }
    }


@router.get("/api/tasks", response_model=List[Task])
def list_tasks(board: str = "tasks", agent: str = None, status: str = None, project_id: int = None):
    """List all tasks with optional filters."""
    with get_db() as conn:
        query = "SELECT * FROM tasks WHERE board = ?"
        params = [board]
        if agent:
            query += " AND agent = ?"
            params.append(agent)
        if status:
            query += " AND status = ?"
            params.append(status)
        if project_id is not None:
            query += " AND project_id = ?"
            params.append(project_id)
        query += " ORDER BY CASE priority WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 ELSE 4 END, created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


@router.get("/api/tasks/{task_id}", response_model=Task)
def get_task(task_id: int):
    """Get a single task."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        return dict(row)


@router.post("/api/tasks", response_model=Task)
async def create_task(task: TaskCreate):
    """Create a new task."""
    print(f"\U0001f4dd CREATE-TASK: {task.title} | Status: {task.status} | Agent: {task.agent} | Priority: {task.priority}")
    now = datetime.now().isoformat()
    try:
        with get_db_write() as conn:
            print(f"\U0001f4be CREATE-TASK: Inserting into database")
            cursor = conn.execute(
                """INSERT INTO tasks (title, description, status, priority, agent, due_date, created_at, updated_at, board, source_file, source_ref, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task.title, task.description, task.status, task.priority, task.agent, task.due_date, now, now, task.board, task.source_file, task.source_ref, task.project_id)
            )
            task_id = cursor.lastrowid
            print(f"\u2705 CREATE-TASK: Database insert successful - Task ID: {task_id}")
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            result = dict(row)
        logger.info(f"Task #{task_id} created by {task.agent}: {task.title}")
        log_activity(task_id, "created", task.agent, f"Created: {task.title}")
    except Exception as e:
        print(f"\u274c CREATE-TASK: Database error - {type(e).__name__}: {e}")
        import traceback
        print(f"\u274c CREATE-TASK: Traceback - {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to create task: {str(e)}")

    print(f"\U0001f4e1 CREATE-TASK: Broadcasting task_created event")
    await manager.broadcast({"type": "task_created", "task": result})
    print(f"\u2705 CREATE-TASK COMPLETE: Task #{task_id}")
    return result


@router.patch("/api/tasks/{task_id}", response_model=Task)
async def update_task(task_id: int, updates: TaskUpdate):
    """Update a task."""
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        current = dict(row)
        changes = []
        update_fields = []
        params = []
        for field in ["title", "description", "status", "priority", "agent", "due_date", "source_file", "source_ref", "project_id"]:
            new_value = getattr(updates, field)
            if new_value is not None and new_value != current[field]:
                update_fields.append(f"{field} = ?")
                params.append(new_value)
                changes.append(f"{field}: {current[field]} \u2192 {new_value}")

        # If moving to Done, also clear working_agent/agent_session_key in same transaction
        moving_to_done = updates.status == "Done" and current.get("status") != "Done"
        if moving_to_done:
            if "working_agent = ?" not in update_fields:
                update_fields.append("working_agent = ?")
                params.append(None)
            if "agent_session_key = ?" not in update_fields:
                update_fields.append("agent_session_key = ?")
                params.append(None)

        if update_fields:
            update_fields.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(task_id)
            conn.execute(f"UPDATE tasks SET {', '.join(update_fields)} WHERE id = ?", params)
            logger.info(f"Task #{task_id} updated by {updates.agent or current['agent']}: {'; '.join(changes)}")

        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = dict(row)

    if changes:
        log_activity(task_id, "updated", updates.agent or current["agent"], "; ".join(changes))

    # Auto-stop agent when task is moved to Done via PATCH
    if moving_to_done and AUTO_STOP_ON_DONE:
        # Use session key from pre-write data (already NULL in DB after write)
        session_key = current.get("agent_session_key")
        if session_key:
            print(f"🛑 Auto-stopping agent session {session_key} for task #{task_id} (PATCH to Done)")
            await stop_agent_session(session_key)
            set_task_session(task_id, None)
            logger.info(f"Task #{task_id} moved to Done — cleared agent session {session_key}")
        await manager.broadcast({"type": "work_stopped", "task_id": task_id, "agent": current.get("working_agent")})
        print(f"🧹 Cleared agent session for task #{task_id} (PATCH)")
        # Refresh result after clearing
        with get_db() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            result = dict(row)

    await manager.broadcast({"type": "task_updated", "task": result})
    return result


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    """Delete a task."""
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        logger.info(f"Task #{task_id} deleted: {row['title']}")
    log_activity(task_id, "deleted", None, f"Deleted: {row['title']}")
    await manager.broadcast({"type": "task_deleted", "task_id": task_id})
    return {"status": "deleted", "id": task_id}


@router.get("/api/agents/{agent}/tasks")
def get_agent_tasks(agent: str):
    """Get all tasks assigned to an agent."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE agent = ? AND status NOT IN ('Done', 'Blocked') ORDER BY priority, created_at",
            (agent,)
        ).fetchall()
        return [dict(row) for row in rows]


@router.post("/api/tasks/{task_id}/start-work")
async def start_work(task_id: int, agent: str):
    """Mark that an agent is actively working on a task. Auto-moves to In Progress."""
    print(f"\U0001f916 START-WORK: Task #{task_id} | Agent: {agent}")
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            print(f"\u274c START-WORK FAILED: Task #{task_id} not found")
            raise HTTPException(status_code=404, detail="Task not found")
        task = dict(row)
        current_status = task["status"]
        current_working = task.get("working_agent")
        now = datetime.now().isoformat()
        print(f"\U0001f4cb START-WORK: Current status: {current_status} | Current working_agent: {current_working}")
        moved = False
        if current_status in ["Backlog", "Blocked"]:
            print(f"\U0001f504 START-WORK: Auto-moving from {current_status} \u2192 In Progress")
            conn.execute(
                "UPDATE tasks SET working_agent = ?, status = ?, updated_at = ? WHERE id = ?",
                (agent, "In Progress", now, task_id)
            )
            moved = True
            logger.info(f"Task #{task_id} status change: {current_status} -> In Progress by {agent} (start-work)")
        else:
            print(f"\U0001f4be START-WORK: Updating working_agent={agent} (status unchanged: {current_status})")
            conn.execute(
                "UPDATE tasks SET working_agent = ?, updated_at = ? WHERE id = ?",
                (agent, now, task_id)
            )
        print(f"\u2705 START-WORK: Database updated successfully")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = dict(row)

    if moved:
        log_activity(task_id, "status_change", agent, f"Auto-moved from {current_status} to In Progress (agent started work)")

    print(f"\U0001f4e1 START-WORK: Broadcasting work_started event")
    await manager.broadcast({"type": "work_started", "task_id": task_id, "agent": agent})
    if moved:
        print(f"\U0001f4e1 START-WORK: Broadcasting task_updated event (status changed)")
        await manager.broadcast({"type": "task_updated", "task": result})

    # Spawn agent if no session exists yet (Play button or manual start-work).
    # If agent was already spawned (e.g. via /move), existing session prevents double-spawn.
    if agent in cfg.AGENT_TO_OPENCLAW_ID and agent != "User":
        existing_session = get_task_session(task_id)
        if not existing_session:
            print(f"🚀 START-WORK: Spawning {agent} for task #{task_id}")
            await spawn_agent_session(task_id, result["title"], result.get("description", ""), agent)
        else:
            print(f"⏩ START-WORK: Session already exists for task #{task_id}: {existing_session}")

    print(f"\u2705 START-WORK COMPLETE: Task #{task_id} | Agent: {agent} | Moved: {moved}")
    return {"status": "working", "task_id": task_id, "agent": agent, "moved_to": "In Progress" if moved else None}


@router.post("/api/tasks/{task_id}/stop-work")
async def stop_work(task_id: int, agent: str = None, outcome: str = None, reason: str = None):
    """Mark that an agent has stopped working on a task."""
    print(f"\U0001f6d1 STOP-WORK: Task #{task_id} | Agent: {agent} | Outcome: {outcome} | Reason: {reason}")
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            print(f"\u274c STOP-WORK FAILED: Task #{task_id} not found")
            raise HTTPException(status_code=404, detail="Task not found")
        task = dict(row)
        now = datetime.now().isoformat()
        current_status = task["status"]
        current_working = task.get("working_agent")
        new_status = None
        action_item = None
        print(f"\U0001f4cb STOP-WORK: Current status: {current_status} | Current working_agent: {current_working}")

        if outcome == "review" and current_status not in ("Review", "Done"):
            new_status = "Review"
            print(f"\U0001f504 STOP-WORK: Auto-moving to Review (outcome=review)")
            reason_text = reason or "Work completed, ready for review"
            cursor = conn.execute(
                "INSERT INTO action_items (task_id, agent, content, item_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, agent or "Agent", reason_text, "completion", now)
            )
            action_item = {"id": cursor.lastrowid, "task_id": task_id, "agent": agent or "Agent",
                          "content": reason_text, "item_type": "completion", "resolved": False, "created_at": now}
            print(f"\U0001f4dd STOP-WORK: Created completion action item #{cursor.lastrowid}")
        elif outcome == "blocked" and current_status == "In Progress":
            new_status = "Blocked"
            print(f"\U0001f504 STOP-WORK: Auto-moving to Blocked (outcome=blocked)")
            reason_text = reason or "Blocked - awaiting input"
            cursor = conn.execute(
                "INSERT INTO action_items (task_id, agent, content, item_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, agent or "Agent", reason_text, "blocker", now)
            )
            action_item = {"id": cursor.lastrowid, "task_id": task_id, "agent": agent or "Agent",
                          "content": reason_text, "item_type": "blocker", "resolved": False, "created_at": now}
            print(f"\U0001f4dd STOP-WORK: Created blocker action item #{cursor.lastrowid}")
        else:
            print(f"\U0001f4be STOP-WORK: Clearing working_agent (no status change)")

        # Keep agent_session_key alive — session stays until Done or explicit UI stop.
        # Only clear working_agent (agent is no longer actively working, but session remains for follow-ups).
        if new_status:
            print(f"💾 STOP-WORK: Updating DB - working_agent=NULL, status={new_status} (session key preserved)")
            conn.execute(
                "UPDATE tasks SET working_agent = NULL, status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, task_id)
            )
            logger.info(f"Task #{task_id} status change: {current_status} -> {new_status} by {agent or 'Agent'} (stop-work)")
        else:
            print(f"💾 STOP-WORK: Updating DB - working_agent=NULL (status unchanged, session key preserved)")
            conn.execute(
                "UPDATE tasks SET working_agent = NULL, updated_at = ? WHERE id = ?",
                (now, task_id)
            )
        print(f"\u2705 STOP-WORK: Database updated successfully")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = dict(row)

    if new_status:
        log_activity(task_id, "status_change", agent or "Agent", f"Auto-moved to {new_status} (agent stopped work)")

    print(f"\U0001f4e1 STOP-WORK: Broadcasting work_stopped event")
    await manager.broadcast({"type": "work_stopped", "task_id": task_id, "agent": agent or current_working})
    if new_status:
        print(f"\U0001f4e1 STOP-WORK: Broadcasting task_updated event (status: {new_status})")
        await manager.broadcast({"type": "task_updated", "task": result})
    if action_item:
        print(f"\U0001f4e1 STOP-WORK: Broadcasting action_item_added event")
        await manager.broadcast({"type": "action_item_added", "task_id": task_id, "item": action_item})

    print(f"\u2705 STOP-WORK COMPLETE: Task #{task_id} | New status: {new_status or current_status}")
    return {"status": "stopped", "task_id": task_id, "moved_to": new_status}


@router.post("/api/tasks/{task_id}/move")
async def move_task(task_id: int, status: str = None, agent: str = None, reason: str = None):
    """Quick move task to a new status with workflow rules."""
    print(f"\U0001f4cb MOVE-TASK: Task #{task_id} \u2192 {status} | Agent: {agent} | Reason: {reason}")
    now = datetime.now().isoformat()
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            print(f"\u274c MOVE-TASK FAILED: Task #{task_id} not found")
            raise HTTPException(status_code=404, detail="Task not found")
        task = dict(row)
        old_status = task["status"]
        old_working = task.get("working_agent")
        print(f"\U0001f4cb MOVE-TASK: Current state - Status: {old_status} | Working: {old_working} | Assigned: {task.get('agent')}")

        if status == "Done" and agent != "User":
            print(f"\u274c MOVE-TASK BLOCKED: Only User can move to Done (agent={agent})")
            raise HTTPException(status_code=403, detail="Only User can move tasks to Done")

        # If moving to Done, clear working_agent in same transaction
        if status == "Done":
            print(f"\U0001f4be MOVE-TASK: Updating status {old_status} \u2192 {status} and clearing working_agent")
            conn.execute("UPDATE tasks SET status = ?, working_agent = NULL, agent_session_key = NULL, updated_at = ? WHERE id = ?", (status, now, task_id))
        else:
            print(f"\U0001f4be MOVE-TASK: Updating status {old_status} \u2192 {status}")
            conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, now, task_id))
        print(f"\u2705 MOVE-TASK: Database updated successfully")

        logger.info(f"Task #{task_id} moved: {old_status} -> {status} by {agent}")

        action_item = None
        if status == "Review" and old_status != "Review":
            content = reason or f"Ready for review: {task['title']}"
            cursor = conn.execute(
                "INSERT INTO action_items (task_id, agent, content, item_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, agent or task["agent"], content, "completion", now)
            )
            action_item = {
                "id": cursor.lastrowid, "task_id": task_id, "agent": agent or task["agent"],
                "content": content, "item_type": "completion", "resolved": 0, "created_at": now
            }
        if status == "Blocked" and old_status != "Blocked":
            content = reason or f"Blocked: {task['title']} - reason not specified"
            cursor = conn.execute(
                "INSERT INTO action_items (task_id, agent, content, item_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, agent or task["agent"], content, "blocker", now)
            )
            action_item = {
                "id": cursor.lastrowid, "task_id": task_id, "agent": agent or task["agent"],
                "content": content, "item_type": "blocker", "resolved": 0, "created_at": now
            }

        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = dict(row)

    log_activity(task_id, "moved", agent, f"Moved to {status}")

    await manager.broadcast({"type": "task_updated", "task": result})
    if action_item:
        await manager.broadcast({"type": "action_item_added", "task_id": task_id, "item": action_item})

    if status == "In Progress" and old_status != "In Progress":
        assigned_agent = result.get("agent", "Unassigned")
        if assigned_agent in cfg.AGENT_TO_OPENCLAW_ID and assigned_agent != "User":
            print(f"🚀 MOVE-TASK: Task #{task_id} moved to In Progress — auto-spawning {assigned_agent}")
            await spawn_agent_session(task_id, result.get("title", ""), result.get("description", ""), assigned_agent)

    session_cleared = False
    if status == "Done":
        await manager.broadcast({"type": "work_stopped", "task_id": task_id, "agent": old_working})
        if AUTO_STOP_ON_DONE:
            # Use session key from pre-write task data (already NULL in DB after write)
            session_key = task.get("agent_session_key")
            if session_key:
                print(f"🛑 Auto-stopping agent session {session_key} for task #{task_id} (moved to Done)")
                await stop_agent_session(session_key)
                set_task_session(task_id, None)
                session_cleared = True
                logger.info(f"Task #{task_id} moved to Done — cleared agent session {session_key}")
                print(f"🧹 Cleared agent session for task #{task_id}")

    return {"status": "moved", "new_status": status, "action_item_created": action_item is not None, "session_cleared": session_cleared}


@router.get("/api/tasks/{task_id}/agent-status")
async def get_agent_status(task_id: int):
    """Check if the agent session for a task is still alive."""
    with get_db() as conn:
        row = conn.execute("SELECT agent_session_key, working_agent FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")

    session_key = row["agent_session_key"]
    working_agent = row["working_agent"]

    if not session_key:
        return {"alive": False, "session_key": None, "working_agent": working_agent}

    alive = await is_session_alive(session_key)
    # Don't clear session key here — it's needed for followup spawning.
    # Session cleanup only happens on Done or explicit Stop.
    return {"alive": alive, "session_key": session_key, "working_agent": working_agent}


@router.get("/api/activity")
def get_activity(limit: int = 50):
    """Get recent activity."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
