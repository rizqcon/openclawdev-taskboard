"""
Chat: command bar history, chat, respond endpoints.
"""

import json
import logging
import secrets
from datetime import datetime
from fastapi import APIRouter, Depends, Header, HTTPException

import httpx

from app.config import (
    OPENCLAW_ENABLED, OPENCLAW_GATEWAY_URL, OPENCLAW_TOKEN,
    TASKBOARD_API_KEY, DATA_DIR,
)
from app.database import get_db, get_db_write
from app.models import ChatMessage, ChatResponse
from app.websocket import manager

logger = logging.getLogger(__name__)
router = APIRouter()


def verify_api_key(authorization: str = Header(None), x_api_key: str = Header(None)):
    """Verify API key from Authorization header or X-API-Key header."""
    if not TASKBOARD_API_KEY:
        return True
    if authorization:
        if authorization.startswith("Bearer "):
            token = authorization[7:]
            if secrets.compare_digest(token, TASKBOARD_API_KEY):
                return True
    if x_api_key:
        if secrets.compare_digest(x_api_key, TASKBOARD_API_KEY):
            return True
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


@router.get("/api/chat/history")
def get_chat_history(limit: int = 100, session: str = "main"):
    """Get command bar chat history from database, filtered by session."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, session_key, role, content, attachments, created_at FROM chat_messages WHERE session_key = ? ORDER BY id DESC LIMIT ?",
            (session, limit)
        ).fetchall()
        messages = []
        for row in reversed(rows):
            msg = {
                "id": row["id"],
                "session_key": row["session_key"],
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["created_at"]
            }
            if row["attachments"]:
                msg["attachments"] = json.loads(row["attachments"])
            messages.append(msg)
        return {"history": messages, "session": session}


@router.post("/api/chat/send")
async def chat_send(msg: ChatMessage):
    """Send a message to the main agent via sessions_send."""
    if not OPENCLAW_ENABLED:
        return {"sent": False, "error": "OpenClaw integration not enabled."}

    now = datetime.now().isoformat()
    message_content = f"System: [TASKBOARD_CHAT] User says: {msg.message}\n\nRespond naturally."

    if msg.attachments:
        import base64 as b64_module
        import uuid

        attachments_dir = DATA_DIR / "attachments"
        attachments_dir.mkdir(exist_ok=True)

        for att in msg.attachments:
            att_type = att.get("type", "")
            att_data = att.get("data", "")
            att_filename = att.get("filename", "file")

            if att_type.startswith("image/") and att_data:
                try:
                    if att_data.startswith("data:") and ";base64," in att_data:
                        header, b64_content = att_data.split(",", 1)
                        mime_type = header.split(":")[1].split(";")[0]
                        ext = mime_type.split("/")[1] if "/" in mime_type else "png"
                    else:
                        b64_content = att_data
                        ext = "png"
                    if ext not in ["png", "jpg", "jpeg", "gif", "webp"]:
                        ext = "png"
                    img_filename = f"{uuid.uuid4().hex[:8]}_{att_filename or 'image'}"
                    if not img_filename.endswith(f".{ext}"):
                        img_filename = f"{img_filename}.{ext}"
                    img_path = attachments_dir / img_filename
                    with open(img_path, "wb") as f:
                        f.write(b64_module.b64decode(b64_content))
                    message_content += f"\n\n\U0001f4f7 **Image attached:** `/app/data/attachments/{img_filename}`\nUse the Read tool to view this image."
                except Exception as e:
                    print(f"Failed to save image attachment: {e}")
                    message_content += f"\n\n[Image attachment failed to save: {e}]"
            elif att_data:
                if att_data.startswith("data:") and ";base64," in att_data:
                    try:
                        import base64
                        b64_content = att_data.split(",", 1)[1]
                        decoded = base64.b64decode(b64_content).decode("utf-8", errors="replace")
                        message_content += f"\n\n**\U0001f4ce Attached file: {att_filename}**\n```\n{decoded}\n```"
                    except Exception as e:
                        message_content += f"\n\n[Attached File: {att_filename} (decode error: {e})]"
                else:
                    message_content += f"\n\n[Attached File: {att_filename}]"

    session_key = msg.session or "main"

    attachments_json = json.dumps(msg.attachments) if msg.attachments else None
    with get_db_write() as conn:
        cursor = conn.execute(
            "INSERT INTO chat_messages (session_key, role, content, attachments, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_key, "user", msg.message, attachments_json, now)
        )
        user_msg_id = cursor.lastrowid

    logger.info(f"Chat message from user in session {session_key}")

    user_msg = {
        "id": user_msg_id, "session_key": session_key, "role": "user",
        "content": msg.message, "timestamp": now, "attachments": msg.attachments
    }
    await manager.broadcast({"type": "command_bar_message", "message": user_msg})

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            payload = {
                "tool": "sessions_send",
                "args": {"message": message_content, "sessionKey": session_key, "timeoutSeconds": 90}
            }
            headers = {"Authorization": f"Bearer {OPENCLAW_TOKEN}", "Content-Type": "application/json"}
            response = await client.post(f"{OPENCLAW_GATEWAY_URL}/tools/invoke", json=payload, headers=headers)

            if response.status_code == 200:
                result = response.json()
                inner = result.get("result", {})
                if isinstance(inner, dict):
                    details = inner.get("details", {})
                    assistant_reply = details.get("reply") or inner.get("reply") or inner.get("response")
                else:
                    assistant_reply = str(inner) if inner else None

                if assistant_reply and not isinstance(assistant_reply, str):
                    import json as json_module
                    assistant_reply = json_module.dumps(assistant_reply) if isinstance(assistant_reply, (dict, list)) else str(assistant_reply)

                if assistant_reply:
                    with get_db_write() as conn:
                        cursor = conn.execute(
                            "INSERT INTO chat_messages (session_key, role, content, attachments, created_at) VALUES (?, ?, ?, ?, ?)",
                            (session_key, "assistant", assistant_reply, None, now)
                        )

                    return {"sent": True, "response": assistant_reply, "session": session_key}

                return {"sent": True, "response": "No response received"}
            else:
                error_text = response.text[:200] if response.text else f"HTTP {response.status_code}"
                return {"sent": False, "error": error_text}
    except Exception as e:
        print(f"Error sending chat message: {e}")
        return {"sent": False, "error": str(e)}


@router.post("/api/chat/respond")
async def chat_respond(msg: ChatResponse, _: bool = Depends(verify_api_key)):
    """Endpoint for agent to push responses back to the command bar. Requires API key."""
    now = datetime.now().isoformat()
    session_key = msg.session or "main"

    with get_db_write() as conn:
        cursor = conn.execute(
            "INSERT INTO chat_messages (session_key, role, content, attachments, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_key, "assistant", msg.response, None, now)
        )
        msg_id = cursor.lastrowid

    logger.info(f"Agent response pushed to session {session_key}")

    agent_msg = {
        "id": msg_id, "session_key": session_key,
        "role": "assistant", "content": msg.response, "timestamp": now
    }
    await manager.broadcast({"type": "command_bar_message", "message": agent_msg})
    return {"delivered": True}


# Legacy endpoints (backward compatibility)
@router.get("/api/jarvis/history")
def get_chat_history_legacy(limit: int = 100, session: str = "main"):
    return get_chat_history(limit, session)

@router.post("/api/jarvis/chat")
async def chat_send_legacy(msg: ChatMessage):
    return await chat_send(msg)

@router.post("/api/jarvis/respond")
async def chat_respond_legacy(msg: ChatResponse, _: bool = Depends(verify_api_key)):
    return await chat_respond(msg, _)

@router.post("/api/molt/chat")
async def chat_molt_legacy(msg: ChatMessage):
    return await chat_send(msg)

@router.post("/api/molt/respond")
async def chat_molt_legacy_respond(msg: ChatResponse, _: bool = Depends(verify_api_key)):
    return await chat_respond(msg, _)
