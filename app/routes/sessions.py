"""
Sessions: list, create, stop, stop-all, delete OpenClaw sessions.
"""

import json
import logging
from datetime import datetime
from fastapi import APIRouter

from app.config import OPENCLAW_ENABLED, OPENCLAW_GATEWAY_URL, OPENCLAW_TOKEN, MAIN_AGENT_NAME, MAIN_AGENT_EMOJI
from app.database import get_db_write
from app.models import SessionCreate
from app.websocket import manager

import httpx

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/sessions")
async def list_sessions():
    """Proxy to OpenClaw sessions_list to get active sessions."""
    if not OPENCLAW_ENABLED:
        return {"sessions": [], "error": "OpenClaw integration not enabled"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {"tool": "sessions_list", "args": {"limit": 20, "messageLimit": 0}}
            headers = {"Authorization": f"Bearer {OPENCLAW_TOKEN}", "Content-Type": "application/json"}
            response = await client.post(f"{OPENCLAW_GATEWAY_URL}/tools/invoke", json=payload, headers=headers)

            if response.status_code == 200:
                result = response.json()
                if result.get("ok"):
                    inner_result = result.get("result", {})
                    content = inner_result.get("content", [])
                    if content and len(content) > 0:
                        text_content = content[0].get("text", "{}")
                        sessions_data = json.loads(text_content)
                    else:
                        sessions_data = inner_result
                    sessions = sessions_data.get("sessions", [])

                    formatted = []
                    for s in sessions:
                        key = s.get("key", "")
                        session_label = s.get("label", "")
                        display = s.get("displayName", key)
                        if key == "main" or key == "agent:main:main":
                            label = f"{MAIN_AGENT_EMOJI} {MAIN_AGENT_NAME} (Main)"
                        elif session_label:
                            label = f"\U0001f916 {session_label}"
                        elif "subagent" in key:
                            short_id = key.split(":")[-1][:8] if ":" in key else key[:8]
                            label = f"\U0001f916 Session {short_id}"
                        elif key.startswith("agent:"):
                            parts = key.split(":")
                            agent_name = parts[1] if len(parts) > 1 else key
                            label = f"\U0001f916 {agent_name.title()}"
                        else:
                            label = display
                        formatted.append({
                            "key": key, "label": label, "channel": s.get("channel", ""),
                            "model": s.get("model", ""), "updatedAt": s.get("updatedAt", 0)
                        })

                    formatted.sort(key=lambda x: (0 if "main" in x["key"].lower() else 1, -x.get("updatedAt", 0)))
                    return {"sessions": formatted}

            return {"sessions": [], "error": f"Failed to fetch sessions: {response.status_code}"}
    except Exception as e:
        print(f"Error fetching sessions: {e}")
        return {"sessions": [], "error": str(e)}


@router.post("/api/sessions/create")
async def create_session(req: SessionCreate):
    """Create a new OpenClaw session via sessions_spawn."""
    if not OPENCLAW_ENABLED:
        return {"success": False, "error": "OpenClaw integration not enabled"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload = {
                "tool": "sessions_spawn",
                "args": {
                    "agentId": req.agentId,
                    "task": req.task,
                    "label": req.label or f"taskboard-{datetime.now().strftime('%H%M%S')}",
                    "cleanup": "keep"
                }
            }
            headers = {"Authorization": f"Bearer {OPENCLAW_TOKEN}", "Content-Type": "application/json"}
            response = await client.post(f"{OPENCLAW_GATEWAY_URL}/tools/invoke", json=payload, headers=headers)
            if response.status_code == 200:
                result = response.json()
                if result.get("ok"):
                    return {"success": True, "result": result.get("result", {})}
            return {"success": False, "error": f"Failed: {response.status_code}"}
    except Exception as e:
        print(f"Error creating session: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/sessions/{session_key}/stop")
async def stop_session(session_key: str):
    """Stop/abort a running session."""
    if not OPENCLAW_ENABLED:
        return {"success": False, "error": "OpenClaw integration not enabled"}
    from app.openclaw import stop_agent_session, set_task_session
    try:
        success = await stop_agent_session(session_key)
        # Clear session key from any task that references it
        with get_db_write() as conn:
            conn.execute(
                "UPDATE tasks SET agent_session_key = NULL WHERE agent_session_key = ?",
                (session_key,)
            )
        print(f"🧹 Cleared DB session key {session_key[:30]}...")
        if success:
            return {"success": True, "message": f"Stopped session: {session_key}"}
        else:
            return {"success": False, "error": f"Failed to stop session: {session_key}"}
    except Exception as e:
        print(f"Error stopping session: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/sessions/stop-all")
async def stop_all_sessions():
    """Emergency stop all non-main sessions."""
    if not OPENCLAW_ENABLED:
        return {"success": False, "error": "OpenClaw integration not enabled"}
    stopped = []
    errors = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {"tool": "sessions_list", "args": {"limit": 50, "messageLimit": 0}}
            headers = {"Authorization": f"Bearer {OPENCLAW_TOKEN}", "Content-Type": "application/json"}
            response = await client.post(f"{OPENCLAW_GATEWAY_URL}/tools/invoke", json=payload, headers=headers)
            if response.status_code == 200:
                result = response.json()
                if result.get("ok"):
                    inner_result = result.get("result", {})
                    content = inner_result.get("content", [])
                    if content and len(content) > 0:
                        text_content = content[0].get("text", "{}")
                        sessions_data = json.loads(text_content)
                    else:
                        sessions_data = inner_result
                    sessions = sessions_data.get("sessions", [])
                    for s in sessions:
                        key = s.get("key", "")
                        if key and "main" not in key.lower():
                            try:
                                stop_result = await stop_session(key)
                                if stop_result.get("success"):
                                    stopped.append(key)
                                else:
                                    errors.append(key)
                            except:
                                errors.append(key)
        return {"success": True, "stopped": stopped, "errors": errors, "message": f"Stopped {len(stopped)} sessions"}
    except Exception as e:
        print(f"Error stopping all sessions: {e}")
        return {"success": False, "error": str(e)}


@router.delete("/api/sessions/{session_key}")
async def delete_session(session_key: str):
    """Delete a session via OpenClaw WebSocket RPC."""
    if not OPENCLAW_ENABLED:
        return {"success": False, "error": "OpenClaw integration not enabled"}

    from app.openclaw import stop_agent_session

    # stop_agent_session uses _ws_rpc("sessions.delete") which fully removes the session
    success = await stop_agent_session(session_key)

    # Clear local references
    with get_db_write() as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_key = ?", (session_key,))
        conn.execute("UPDATE tasks SET agent_session_key = NULL WHERE agent_session_key = ?", (session_key,))

    logger.info(f"Session {session_key} deleted (openclaw={'ok' if success else 'failed'})")
    await manager.broadcast({"type": "session_deleted", "session_key": session_key})
    return {"success": True, "message": f"Deleted session: {session_key}"}
