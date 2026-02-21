"""
Comments: GET/POST/DELETE for task comments, with @mention spawning and agent relay.
"""

import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException

import app.config as cfg
from app.database import get_db, get_db_write
from app.models import CommentCreate
from app.websocket import manager
from app.openclaw import (
    notify_OPENCLAW, send_to_agent_session, spawn_mentioned_agent,
    spawn_followup_session,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/tasks/{task_id}/comments")
def get_comments(task_id: int):
    """Get comments for a task."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM comments WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,)
        ).fetchall()
        return [dict(row) for row in rows]


@router.post("/api/tasks/{task_id}/comments")
async def add_comment(task_id: int, comment: CommentCreate):
    """Add a comment to a task."""
    now = datetime.now().isoformat()
    task_title = ""
    task_status = ""
    agent_session = None

    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")

        task_title = row["title"]
        task_status = row["status"]
        agent_session = row["agent_session_key"] if "agent_session_key" in row.keys() else None

        cursor = conn.execute(
            "INSERT INTO comments (task_id, agent, content, created_at) VALUES (?, ?, ?, ?)",
            (task_id, comment.agent, comment.content, now)
        )

        result = {
            "id": cursor.lastrowid,
            "task_id": task_id,
            "agent": comment.agent,
            "content": comment.content,
            "created_at": now
        }

        working_agent_cleared = None
        if comment.agent and comment.agent != "User":
            task_row = conn.execute("SELECT working_agent FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task_row and task_row["working_agent"] == comment.agent:
                conn.execute(
                    "UPDATE tasks SET working_agent = NULL, updated_at = ? WHERE id = ?",
                    (now, task_id)
                )
                working_agent_cleared = comment.agent

    logger.info(f"Comment added on task #{task_id} by {comment.agent}")
    await manager.broadcast({"type": "comment_added", "task_id": task_id, "comment": result})

    if working_agent_cleared:
        await manager.broadcast({"type": "work_stopped", "task_id": task_id, "agent": working_agent_cleared})

    # Check for @mentions
    mentions = cfg.MENTION_PATTERN.findall(comment.content)
    if mentions:
        task_description = ""
        previous_context = ""
        with get_db() as conn:
            task_row = conn.execute("SELECT description FROM tasks WHERE id = ?", (task_id,)).fetchone()
            task_description = task_row["description"] if task_row else ""
            comment_rows = conn.execute(
                "SELECT agent, content FROM comments WHERE task_id = ? AND id != ? ORDER BY created_at DESC LIMIT 5",
                (task_id, result["id"])
            ).fetchall()
            if comment_rows:
                previous_context = "\n".join([f"**{r['agent']}:** {r['content'][:500]}" for r in reversed(comment_rows)])

        for mentioned_agent in set(mentions):
            matched_agent = None
            for agent_name in cfg.AGENT_TO_OPENCLAW_ID.keys():
                if agent_name.lower() == mentioned_agent.lower():
                    matched_agent = agent_name
                    break
            if matched_agent and matched_agent != comment.agent:
                agent_id = cfg.AGENT_TO_OPENCLAW_ID.get(matched_agent)
                if agent_id:
                    await spawn_mentioned_agent(
                        task_id=task_id,
                        task_title=task_title,
                        task_description=task_description,
                        mentioned_agent=matched_agent,
                        mentioner=comment.agent,
                        comment_content=comment.content,
                        previous_context=previous_context
                    )
                    print(f"\U0001f4e2 Spawned {matched_agent} for mention in task #{task_id}")

    # If from User, relay to agent session or spawn followup
    if comment.agent == "User" and not mentions:
        assigned_agent = None
        with get_db() as conn:
            row = conn.execute("SELECT agent FROM tasks WHERE id = ?", (task_id,)).fetchone()
            assigned_agent = row["agent"] if row else None

        if assigned_agent and assigned_agent in cfg.AGENT_TO_OPENCLAW_ID and assigned_agent != "User":
            sent = False
            if agent_session:
                message = f"💬 **User replied on Task #{task_id}:**\n\n{comment.content}\n\n---\nRespond by posting a comment to the task."
                sent = await send_to_agent_session(agent_session, message)
                if sent:
                    print(f"📨 Relayed user comment to session {agent_session} for task #{task_id}")

            if not sent:
                # Session dead or missing — spawn a followup session
                print(f"🔄 Spawning followup session for task #{task_id} (assigned: {assigned_agent})")
                previous_comments = []
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT agent, content FROM comments WHERE task_id = ? ORDER BY created_at DESC LIMIT 5",
                        (task_id,)
                    ).fetchall()
                    previous_comments = [f"**{r['agent']}:** {r['content'][:500]}" for r in reversed(rows)]
                context = "\n".join(previous_comments[:-1])
                await spawn_followup_session(task_id, task_title, assigned_agent, context, comment.content)
    elif comment.agent not in ["System", "User"] + list(cfg.AGENT_TO_OPENCLAW_ID.keys()):
        await notify_OPENCLAW(task_id, task_title, comment.agent, comment.content)

    return result


@router.delete("/api/tasks/{task_id}/comments/{comment_id}")
async def delete_comment(task_id: int, comment_id: int):
    """Delete a comment from a task."""
    with get_db_write() as conn:
        row = conn.execute(
            "SELECT id FROM comments WHERE id = ? AND task_id = ?",
            (comment_id, task_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Comment not found")
        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))

    logger.info(f"Comment #{comment_id} deleted from task #{task_id}")
    await manager.broadcast({
        "type": "comment_deleted",
        "task_id": task_id,
        "comment_id": comment_id
    })
    return {"status": "deleted", "comment_id": comment_id}
