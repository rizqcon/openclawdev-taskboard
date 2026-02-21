"""
OpenClaw/agent integration: spawn, send, stop, prompts, guardrails.
"""

import asyncio
import httpx
import json as _json
import uuid
import websockets
from typing import Optional
from datetime import datetime

import app.config as cfg
from app.config import (
    OPENCLAW_ENABLED, OPENCLAW_GATEWAY_URL, OPENCLAW_TOKEN,
    TASKBOARD_BASE_URL,
    MAIN_AGENT_NAME, MAIN_AGENT_EMOJI, HUMAN_SUPERVISOR_LABEL,
    PROJECT_NAME, COMPANY_NAME, COMPANY_CONTEXT,
    ALLOWED_PATHS, COMPLIANCE_FRAMEWORKS,
)
from app.database import get_db


# =============================================================================
# SECURITY HELPERS (notify, send, session management)
# =============================================================================

async def notify_OPENCLAW(task_id: int, task_title: str, comment_agent: str, comment_content: str):
    """Send webhook to OpenClaw when a comment needs attention."""
    if not OPENCLAW_ENABLED or comment_agent == MAIN_AGENT_NAME:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            payload = {
                "action": "wake",
                "text": f"\U0001f4ac Task Board: New comment on #{task_id} ({task_title}) from {comment_agent}:\n\n{comment_content[:200]}{'...' if len(comment_content) > 200 else ''}\n\nCheck and respond: {TASKBOARD_BASE_URL}"
            }
            headers = {
                "Authorization": f"Bearer {OPENCLAW_TOKEN}",
                "Content-Type": "application/json"
            }
            await client.post(f"{OPENCLAW_GATEWAY_URL}/api/cron/wake", json=payload, headers=headers)
            print(f"Notified OPENCLAW about comment from {comment_agent}")
    except Exception as e:
        print(f"Webhook to OPENCLAW failed: {e}")


async def send_to_agent_session(session_key: str, message: str) -> bool:
    """Send a follow-up message to an active agent session."""
    if not OPENCLAW_ENABLED or not session_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload = {
                "tool": "sessions_send",
                "args": {
                    "sessionKey": session_key,
                    "message": message
                }
            }
            headers = {
                "Authorization": f"Bearer {OPENCLAW_TOKEN}",
                "Content-Type": "application/json"
            }
            response = await client.post(
                f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            result = response.json() if response.status_code == 200 else None
            if result and result.get("ok"):
                print(f"\u2705 Sent message to session {session_key}")
                return True
            else:
                print(f"\u274c Failed to send to session: {response.text}")
                return False
    except Exception as e:
        print(f"\u274c Failed to send to agent session: {e}")
        return False


def get_task_session(task_id: int) -> Optional[str]:
    """Get the active agent session key for a task."""
    with get_db() as conn:
        row = conn.execute("SELECT agent_session_key FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row["agent_session_key"] if row and row["agent_session_key"] else None


def set_task_session(task_id: int, session_key: Optional[str]):
    """Set or clear the agent session key for a task."""
    from app.database import get_db_write
    with get_db_write() as conn:
        conn.execute(
            "UPDATE tasks SET agent_session_key = ?, updated_at = ? WHERE id = ?",
            (session_key, datetime.now().isoformat(), task_id)
        )


# =============================================================================
# GUARDRAILS & PROMPTS
# =============================================================================

AGENT_GUARDRAILS = f"""
\u26a0\ufe0f MANDATORY CONSTRAINTS (Approved by User via Task Board assignment):

FILESYSTEM BOUNDARIES:
- ONLY access: {ALLOWED_PATHS}
- Everything else is FORBIDDEN without explicit authorization

FORBIDDEN ACTIONS (do not attempt without approval):
- Browser tool (except UX Manager on localhost only)
- git commit (requires safeword from User)
- Any action outside the authorized paths

WEB_FETCH (requires approval):
- You have web_fetch available but MUST ask User first
- Create an action item (type: question) explaining what URL you need and why
- Wait for User to resolve the action item before fetching
- Only fetch after explicit approval

COMPLIANCE CONTEXT:
- {COMPANY_NAME}, {COMPANY_CONTEXT}
- {COMPLIANCE_FRAMEWORKS}
- Security over convenience \u2014 always

COMMUNICATION & ESCALATION:
- Post comments on the task card to communicate
- Create action items for questions that need answers (type: question)
- Create action items for blockers (type: blocker)

ESCALATION CHAIN:
1. {MAIN_AGENT_NAME} (coordinator) monitors your action items and may answer if confident
2. If {MAIN_AGENT_NAME} answers, the item gets resolved and you can proceed
3. If {MAIN_AGENT_NAME} is unsure, they leave it for {HUMAN_SUPERVISOR_LABEL} to review
4. {HUMAN_SUPERVISOR_LABEL} has final authority on all decisions

TASK BOARD INTEGRATION:
- Use start-work API when beginning: POST {TASKBOARD_BASE_URL}/api/tasks/{{task_id}}/start-work?agent={{your_name}}
- Post updates as comments: POST {TASKBOARD_BASE_URL}/api/tasks/{{task_id}}/comments (json: {{"agent": "your_name", "content": "message"}})
- Create action items for questions: POST {TASKBOARD_BASE_URL}/api/tasks/{{task_id}}/action-items (json: {{"agent": "your_name", "content": "question", "item_type": "question"}})
- Move to Review when done: POST {TASKBOARD_BASE_URL}/api/tasks/{{task_id}}/move?status=Review&agent={{your_name}}&reason=...
- Use stop-work API when finished: POST {TASKBOARD_BASE_URL}/api/tasks/{{task_id}}/stop-work

REPORT FORMAT:
When complete, post a comment with your findings using this format:
## [Your Role] Report
**Task:** [task title]
**Verdict:** \u2705 APPROVED / \u26a0\ufe0f CONCERNS / \U0001f6d1 BLOCKED
### Findings
- [SEVERITY] Issue description
### Summary
[1-2 sentence assessment]
"""


async def is_session_alive(session_key: str) -> bool:
    """Check if an OpenClaw session is still active via sessions_list."""
    if not OPENCLAW_ENABLED or not session_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "tool": "sessions_list",
                "args": {}
            }
            headers = {
                "Authorization": f"Bearer {OPENCLAW_TOKEN}",
                "Content-Type": "application/json"
            }
            response = await client.post(
                f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            result = response.json() if response.status_code == 200 else None
            if result and result.get("ok"):
                sessions = result.get("result", [])
                # Handle various response formats
                if isinstance(sessions, dict):
                    # Could be {"sessions": [...]} or {"content": [...]}
                    sessions = sessions.get("sessions", sessions.get("content", []))
                # If result is a list of content blocks (tool response format)
                if isinstance(sessions, list) and len(sessions) > 0 and isinstance(sessions[0], dict) and "text" in sessions[0]:
                    import json as _json
                    try:
                        parsed = _json.loads(sessions[0]["text"])
                        if isinstance(parsed, list):
                            sessions = parsed
                        elif isinstance(parsed, dict):
                            sessions = parsed.get("sessions", [])
                    except (ValueError, KeyError):
                        pass
                print(f"🔍 is_session_alive: Looking for {session_key} in {len(sessions)} sessions")
                for s in sessions:
                    key = s.get("sessionKey") or s.get("key") or s.get("id", "")
                    if key == session_key:
                        status = s.get("status", "").lower()
                        print(f"🔍 is_session_alive: Found! Status={status}")
                        return status not in ("stopped", "dead", "terminated", "error")
                print(f"🔍 is_session_alive: Session {session_key} NOT found in list")
            else:
                print(f"🔍 is_session_alive: API response not ok: {result}")
            return False
    except Exception as e:
        print(f"⚠️  is_session_alive check failed: {e}")
        return False


def _get_ws_url() -> str:
    """Derive WebSocket URL from gateway HTTP URL."""
    url = OPENCLAW_GATEWAY_URL.replace("http://", "ws://").replace("https://", "wss://")
    return url.rstrip("/") + "/ws"


async def _ws_rpc(method: str, params: dict, timeout: float = 10.0) -> dict:
    """Send a single JSON-RPC-style request over WebSocket to OpenClaw gateway."""
    ws_url = _get_ws_url()
    req_id = str(uuid.uuid4())
    msg = _json.dumps({"type": "req", "id": req_id, "method": method, "params": params})
    print(f"🔌 WS-RPC: Connecting to {ws_url}")
    print(f"🔌 WS-RPC: Sending {method} → {_json.dumps(params)[:200]}")
    async with websockets.connect(
        ws_url,
        origin=OPENCLAW_GATEWAY_URL,
        open_timeout=5,
        close_timeout=5,
    ) as ws:
        # Step 1: Wait for connect.challenge event from server
        nonce = None
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            data = _json.loads(raw)
            print(f"🔌 WS-RPC: Recv type={data.get('type')} event={data.get('event', '')}")
            if data.get("type") == "event" and data.get("event") == "connect.challenge":
                nonce = data.get("payload", {}).get("nonce", "")
                print(f"🔌 WS-RPC: Got challenge nonce={nonce[:8]}...")
                break

        # Step 2: Send connect with auth token
        connect_id = str(uuid.uuid4())
        connect_msg = _json.dumps({
            "type": "req",
            "id": connect_id,
            "method": "connect",
            "params": {
                "minProtocol": 3,
                "maxProtocol": 3,
                "client": {"id": "openclaw-control-ui", "version": "dev", "platform": "linux", "mode": "webchat"},
                "role": "operator",
                "scopes": ["operator.admin", "operator.read", "operator.approvals", "operator.pairing"],
                "auth": {"token": OPENCLAW_TOKEN},
            }
        })
        await ws.send(connect_msg)
        print(f"🔌 WS-RPC: Sent connect (id={connect_id[:8]}...)")

        # Step 3: Wait for connect response
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            data = _json.loads(raw)
            print(f"🔌 WS-RPC: Handshake recv type={data.get('type')} ok={data.get('ok')}")
            if data.get("id") == connect_id:
                if data.get("ok") is False:
                    print(f"🔌 WS-RPC: Connect failed: {data.get('error')}")
                    return {}
                print(f"🔌 WS-RPC: Connected!")
                break

        # Step 4: Send actual request
        await ws.send(msg)
        print(f"🔌 WS-RPC: Sent {method} (id={req_id[:8]}...)")
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                data = _json.loads(raw)
                msg_type = data.get("type", "")
                print(f"🔌 WS-RPC: Recv type={msg_type} id={str(data.get('id',''))[:8]}")
                if data.get("id") == req_id:
                    print(f"🔌 WS-RPC: Response: {_json.dumps(data)[:500]}")
                    return data
            except asyncio.TimeoutError:
                print(f"🔌 WS-RPC: Timeout waiting for response")
                break
    print(f"🔌 WS-RPC: No matching response received")
    return {}


async def fetch_agent_identities(agents_data: list) -> None:
    """Fetch emoji/avatar for each agent via WS-RPC agent.identity.get and update AGENT_META."""
    if not OPENCLAW_ENABLED:
        return
    for agent in agents_data:
        agent_id = agent["id"]
        agent_name = agent["name"]
        if agent_id == "main":
            agent_name = MAIN_AGENT_NAME
        try:
            result = await _ws_rpc("agent.identity.get", {"agentId": agent_id}, timeout=5)
            payload = result.get("payload", {})
            if payload:
                emoji = payload.get("emoji") or payload.get("avatar")
                if emoji and agent_name in cfg.AGENT_META:
                    cfg.AGENT_META[agent_name]["icon"] = emoji
                    print(f"  🎨 {agent_name}: emoji={emoji}")
        except Exception as e:
            print(f"  ⚠️ Could not fetch identity for {agent_id}: {e}")


async def stop_agent_session(session_key: str) -> bool:
    """Stop an OpenClaw agent session via WebSocket RPC sessions.delete."""
    if not OPENCLAW_ENABLED or not session_key:
        print(f"⚠️ stop_agent_session: Skipped (enabled={OPENCLAW_ENABLED}, key={session_key})")
        return False
    print(f"🛑 stop_agent_session: Deleting session {session_key}")
    try:
        result = await _ws_rpc("sessions.delete", {"key": session_key, "deleteTranscript": True})
        if result.get("type") == "resp" or result.get("result") is not None:
            print(f"✅ Deleted session {session_key} via WS RPC")
            return True
        elif result.get("error"):
            print(f"❌ WS RPC sessions.delete error: {_json.dumps(result.get('error'))}")
            return False
        else:
            print(f"⚠️ WS RPC sessions.delete — no matching response: {_json.dumps(result)[:300]}")
            return False
    except Exception as e:
        print(f"❌ Failed to delete agent session via WS: {type(e).__name__}: {e}")
        return False


_spawning_tasks = set()


async def spawn_agent_session(task_id: int, task_title: str, task_description: str, agent_name: str):
    """Spawn an OpenClaw sub-agent session for a task via tools/invoke API."""
    print(f"\U0001f680 SPAWN-AGENT: Task #{task_id} | Agent: {agent_name}")

    if task_id in _spawning_tasks:
        print(f"\u23e9 SPAWN-AGENT SKIPPED: Already spawning for task #{task_id}")
        return None
    _spawning_tasks.add(task_id)

    try:
        return await _do_spawn_agent_session(task_id, task_title, task_description, agent_name)
    finally:
        _spawning_tasks.discard(task_id)


async def _do_spawn_agent_session(task_id: int, task_title: str, task_description: str, agent_name: str):
    """Internal spawn implementation."""
    if not OPENCLAW_ENABLED:
        print(f"\u26a0\ufe0f  SPAWN-AGENT SKIPPED: OpenClaw not enabled (OPENCLAW_TOKEN not set)")
        return None

    # Guard against double-spawn: check if session already exists and is alive
    existing_key = get_task_session(task_id)
    if existing_key:
        alive = await is_session_alive(existing_key)
        if alive:
            print(f"ℹ️  SPAWN-AGENT SKIPPED: Task #{task_id} already has live session {existing_key}")
            return None
        else:
            print(f"🧹 SPAWN-AGENT: Clearing dead session {existing_key} for task #{task_id}")
            set_task_session(task_id, None)

    agent_id = cfg.AGENT_TO_OPENCLAW_ID.get(agent_name)
    if not agent_id:
        print(f"\u26a0\ufe0f  SPAWN-AGENT SKIPPED: Unknown agent '{agent_name}' (not in cfg.AGENT_TO_OPENCLAW_ID)")
        return None

    print(f"\U0001f50d SPAWN-AGENT: Mapped {agent_name} \u2192 {agent_id}")

    task_prompt = f"""# Task Assignment from RIZQ Task Board (Approved by {HUMAN_SUPERVISOR_LABEL})

**Task #{task_id}:** {task_title}

**Description:**
{task_description or 'No description provided.'}

{AGENT_GUARDRAILS}

## API Base URL (MANDATORY \u2014 do NOT use localhost or 127.0.0.1)
All Task Board API calls MUST use this base URL: {TASKBOARD_BASE_URL}
Do NOT use localhost, 127.0.0.1, or any other address. The task board is ONLY reachable at {TASKBOARD_BASE_URL}.

## Instructions
1. Call start-work API: POST {TASKBOARD_BASE_URL}/api/tasks/{task_id}/start-work?agent={agent_name}
   - This auto-moves the card to "In Progress" if needed
2. Analyze the task thoroughly
3. Post your findings as a comment: POST {TASKBOARD_BASE_URL}/api/tasks/{task_id}/comments (json: {{"agent": "{agent_name}", "content": "your message"}})
4. When done, call stop-work with outcome: POST {TASKBOARD_BASE_URL}/api/tasks/{task_id}/stop-work?agent={agent_name}&outcome=review&reason=<summary>
   - Use outcome=review when work is complete (auto-moves to Review)
   - Use outcome=blocked&reason=<why> if you need input (auto-moves to Blocked)

## IMPORTANT: Stay Available
After posting your findings, **remain available for follow-up questions**. User may reply with questions or requests for clarification. When you receive a message starting with "\U0001f4ac **User replied**", respond thoughtfully and post your response as a comment on the task.

Your session will automatically end when User marks the task as Done.

Begin now.
"""

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            payload = {
                "tool": "sessions_spawn",
                "args": {
                    "agentId": agent_id,
                    "task": task_prompt,
                    "label": f"task-{task_id}",
                    "cleanup": "keep"
                }
            }
            headers = {
                "Authorization": f"Bearer {OPENCLAW_TOKEN}",
                "Content-Type": "application/json"
            }

            print(f"\U0001f4e1 SPAWN-AGENT: Calling OpenClaw API - {OPENCLAW_GATEWAY_URL}/tools/invoke")
            print(f"\U0001f4e6 SPAWN-AGENT: Payload - tool: sessions_spawn, agentId: {agent_id}, label: task-{task_id}")

            response = await client.post(
                f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )

            print(f"\U0001f4e5 SPAWN-AGENT: Response status: {response.status_code}")

            result = response.json() if response.status_code == 200 else None
            print(f"\U0001f4e5 SPAWN-AGENT: Response body: {result}")

            if result and result.get("ok"):
                raw_result = result.get("result", {})
                spawn_info = raw_result.get("details", raw_result)
                run_id = spawn_info.get("runId", "unknown")
                session_key = spawn_info.get("childSessionKey", None)

                print(f"\u2705 SPAWN-AGENT SUCCESS: {agent_name} ({agent_id}) for task #{task_id}")
                print(f"\U0001f4cb SPAWN-AGENT: Session key: {session_key} | Run ID: {run_id}")

                if session_key:
                    print(f"\U0001f4be SPAWN-AGENT: Saving session key to database")
                    set_task_session(task_id, session_key)
                else:
                    print(f"\u26a0\ufe0f  SPAWN-AGENT: No session key in response!")

                print(f"\U0001f4ac SPAWN-AGENT: Posting spawn notification comment")
                async with httpx.AsyncClient(timeout=5.0) as comment_client:
                    await comment_client.post(
                        f"{TASKBOARD_BASE_URL}/api/tasks/{task_id}/comments",
                        json={
                            "agent": "System",
                            "content": f"\U0001f916 **{agent_name}** agent spawned automatically.\n\nSession: `{session_key or 'unknown'}`\nRun ID: `{run_id}`\n\n\U0001f4ac *Reply to this task and the agent will respond.*"
                        }
                    )
                print(f"\u2705 SPAWN-AGENT COMPLETE")
                return result
            else:
                error_msg = response.text if response.status_code != 200 else result
                print(f"\u274c SPAWN-AGENT FAILED: Status {response.status_code}")
                print(f"\u274c SPAWN-AGENT ERROR: {error_msg}")
                return None
    except Exception as e:
        print(f"\u274c SPAWN-AGENT EXCEPTION: {type(e).__name__}: {e}")
        import traceback
        print(f"\u274c SPAWN-AGENT TRACEBACK: {traceback.format_exc()}")
        return None


async def spawn_followup_session(task_id: int, task_title: str, agent_name: str, previous_context: str, new_message: str):
    """Spawn a follow-up session for an agent with conversation context."""
    if not OPENCLAW_ENABLED:
        return None

    # In-flight guard: reuse same set as spawn_agent_session
    if task_id in _spawning_tasks:
        print(f"⏩ FOLLOWUP SKIPPED: Already spawning for task #{task_id}")
        return None

    # Double-spawn guard: check if session already exists and is alive
    existing_key = get_task_session(task_id)
    if existing_key:
        alive = await is_session_alive(existing_key)
        if alive:
            print(f"ℹ️  FOLLOWUP SKIPPED: Task #{task_id} already has live session {existing_key}, sending message instead")
            sent = await send_to_agent_session(existing_key, f"💬 **User replied on Task #{task_id}:**\n\n{new_message}\n\n---\nRespond by posting a comment to the task.")
            if sent:
                return None
            # If send failed, session is dead — clear and continue to spawn
            print(f"🧹 FOLLOWUP: Session {existing_key} unresponsive, clearing")
            set_task_session(task_id, None)

    _spawning_tasks.add(task_id)
    try:
        return await _do_spawn_followup(task_id, task_title, agent_name, previous_context, new_message)
    finally:
        _spawning_tasks.discard(task_id)


async def _do_spawn_followup(task_id: int, task_title: str, agent_name: str, previous_context: str, new_message: str):
    agent_id = cfg.AGENT_TO_OPENCLAW_ID.get(agent_name)
    if not agent_id:
        return None

    followup_prompt = f"""# Follow-up on Task #{task_id}: {task_title}

You previously worked on this task and moved it to Review. User has a follow-up question.

## Previous Conversation:
{previous_context if previous_context else "(No previous messages)"}

## User's New Message:
{new_message}

## API Base URL (MANDATORY \u2014 do NOT use localhost or 127.0.0.1)
All Task Board API calls MUST use this base URL: {TASKBOARD_BASE_URL}

## Instructions:
1. Call start-work API: POST {TASKBOARD_BASE_URL}/api/tasks/{task_id}/start-work?agent={agent_name}
2. Read the context and User's question
3. Respond helpfully by posting a comment: POST {TASKBOARD_BASE_URL}/api/tasks/{task_id}/comments (json: {{"agent": "{agent_name}", "content": "your message"}})
4. Keep your response focused on what User asked
5. Call stop-work API: POST {TASKBOARD_BASE_URL}/api/tasks/{task_id}/stop-work?agent={agent_name}
   - Add &outcome=review&reason=<summary> if work is complete
   - Add &outcome=blocked&reason=<why> if you need more input

Respond now.
"""

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            payload = {
                "tool": "sessions_spawn",
                "args": {
                    "agentId": agent_id,
                    "task": followup_prompt,
                    "label": f"task-{task_id}-followup",
                    "cleanup": "keep"
                }
            }
            headers = {
                "Authorization": f"Bearer {OPENCLAW_TOKEN}",
                "Content-Type": "application/json"
            }
            response = await client.post(
                f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            result = response.json() if response.status_code == 200 else None
            if result and result.get("ok"):
                raw_result = result.get("result", {})
                spawn_info = raw_result.get("details", raw_result)
                session_key = spawn_info.get("childSessionKey", None)
                if session_key:
                    set_task_session(task_id, session_key)
                print(f"\u2705 Spawned follow-up session for {agent_name} on task #{task_id}")
                return result
            else:
                print(f"\u274c Failed to spawn follow-up: {response.text}")
                return None
    except Exception as e:
        print(f"\u274c Failed to spawn follow-up session: {e}")
        return None


async def spawn_mentioned_agent(task_id: int, task_title: str, task_description: str,
                                 mentioned_agent: str, mentioner: str, comment_content: str,
                                 previous_context: str = ""):
    """Spawn a session for an @mentioned agent to contribute to a task they don't own."""
    if not OPENCLAW_ENABLED:
        return None

    agent_id = cfg.AGENT_TO_OPENCLAW_ID.get(mentioned_agent)
    if not agent_id:
        return None

    mention_prompt = f"""# You've Been Tagged: Task #{task_id}

**{mentioner}** mentioned you on a task and needs your input.

## Task: {task_title}
{task_description or '(No description)'}

## What {mentioner} Said:
{comment_content}

## Previous Conversation:
{previous_context if previous_context else "(No prior comments)"}

## Instructions:
1. Call start-work API: POST {TASKBOARD_BASE_URL}/api/tasks/{task_id}/start-work?agent={mentioned_agent}
2. Review the task from YOUR perspective ({mentioned_agent})
3. Post your findings/response as a comment: POST {TASKBOARD_BASE_URL}/api/tasks/{task_id}/comments
4. Call stop-work API: POST {TASKBOARD_BASE_URL}/api/tasks/{task_id}/stop-work?agent={mentioned_agent}

**Note:** You are NOT the assigned owner of this task. You're providing your expertise because you were tagged.
Do NOT move the task (no outcome param) \u2014 that's the owner's job.

{AGENT_GUARDRAILS}

Respond now with your assessment.
"""

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            payload = {
                "tool": "sessions_spawn",
                "args": {
                    "agentId": agent_id,
                    "task": mention_prompt,
                    "label": f"task-{task_id}-mention-{agent_id}",
                    "cleanup": "delete"
                }
            }
            headers = {
                "Authorization": f"Bearer {OPENCLAW_TOKEN}",
                "Content-Type": "application/json"
            }
            response = await client.post(
                f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            result = response.json() if response.status_code == 200 else None
            if result and result.get("ok"):
                raw_result = result.get("result", {})
                spawn_info = raw_result.get("details", raw_result)
                session_key = spawn_info.get("childSessionKey", "unknown")

                async with httpx.AsyncClient(timeout=5.0) as comment_client:
                    await comment_client.post(
                        f"{TASKBOARD_BASE_URL}/api/tasks/{task_id}/comments",
                        json={
                            "agent": "System",
                            "content": f"\U0001f4e2 **{mentioned_agent}** was tagged by {mentioner} and is now reviewing this task."
                        }
                    )

                print(f"\u2705 Spawned {mentioned_agent} for mention on task #{task_id}")
                return result
            else:
                print(f"\u274c Failed to spawn {mentioned_agent} for mention: {response.text}")
                return None
    except Exception as e:
        print(f"\u274c Failed to spawn mentioned agent: {e}")
        return None
