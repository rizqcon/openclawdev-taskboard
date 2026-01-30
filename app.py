"""
RIZQ Task Board - FastAPI Backend
Simple, fast, full agent control, LIVE updates
"""

import sqlite3
import json
import asyncio
import re
import httpx
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Set
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Header, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

# =============================================================================
# CONFIG
# =============================================================================
import os
import secrets
import hashlib

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "tasks.db"
STATIC_PATH = Path(__file__).parent / "static"

# =============================================================================
# BRANDING (configurable via environment variables)
# =============================================================================
MAIN_AGENT_NAME = os.getenv("MAIN_AGENT_NAME", "Jarvis")
MAIN_AGENT_EMOJI = os.getenv("MAIN_AGENT_EMOJI", "\U0001F6E1")
HUMAN_NAME = os.getenv("HUMAN_NAME", "User")
HUMAN_SUPERVISOR_LABEL = os.getenv("HUMAN_SUPERVISOR_LABEL", "User")
BOARD_TITLE = os.getenv("BOARD_TITLE", "Task Board")

AGENTS = [MAIN_AGENT_NAME, "Architect", "Security Auditor", "Code Reviewer", "UX Manager", "User", "Unassigned"]
STATUSES = ["Backlog", "In Progress", "Review", "Done", "Blocked"]
PRIORITIES = ["Critical", "High", "Medium", "Low"]

# Map task board agent names to Clawdbot agent IDs
# Customize these to match your Clawdbot agent configuration
AGENT_TO_CLAWDBOT_ID = {
    MAIN_AGENT_NAME: "main",  # Main agent (handles command bar chat)
    "Architect": "architect",
    "Security Auditor": "security-auditor",
    "Code Reviewer": "code-reviewer",
    "UX Manager": "ux-manager",
}

# Alias for backward compatibility
AGENT_TO_MOLTBOT_ID = AGENT_TO_CLAWDBOT_ID

# Build mention regex dynamically from agent names (including main agent now)
MENTIONABLE_AGENTS = list(AGENT_TO_CLAWDBOT_ID.keys())
MENTION_PATTERN = re.compile(r'@(' + '|'.join(re.escape(a) for a in MENTIONABLE_AGENTS) + r')', re.IGNORECASE)

# Security: Load secrets from environment variables
MOLTBOT_GATEWAY_URL = os.getenv("MOLTBOT_GATEWAY_URL", "http://host.docker.internal:18789")
MOLTBOT_TOKEN = os.getenv("MOLTBOT_TOKEN", "")
TASKBOARD_API_KEY = os.getenv("TASKBOARD_API_KEY", "")
MOLTBOT_ENABLED = bool(MOLTBOT_TOKEN)

# Project configuration (customize in .env)
PROJECT_NAME = os.getenv("PROJECT_NAME", "My Project")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Acme Corp")
COMPANY_CONTEXT = os.getenv("COMPANY_CONTEXT", "software development")
ALLOWED_PATHS = os.getenv("ALLOWED_PATHS", "/workspace, /project")
COMPLIANCE_FRAMEWORKS = os.getenv("COMPLIANCE_FRAMEWORKS", "your security requirements")

# Warn if running without security
if not TASKBOARD_API_KEY:
    print("⚠️  WARNING: TASKBOARD_API_KEY not set. API authentication disabled!")
if not MOLTBOT_TOKEN:
    print("⚠️  WARNING: MOLTBOT_TOKEN not set. MOLTBOT integration disabled!")

# File upload limits
MAX_ATTACHMENT_SIZE_MB = 10
MAX_ATTACHMENT_SIZE_BYTES = MAX_ATTACHMENT_SIZE_MB * 1024 * 1024

# =============================================================================
# SECURITY
# =============================================================================

def verify_api_key(authorization: str = Header(None), x_api_key: str = Header(None)):
    """Verify API key from Authorization header or X-API-Key header."""
    if not TASKBOARD_API_KEY:
        return True  # Auth disabled if no key configured
    
    # Check Authorization: Bearer <token>
    if authorization:
        if authorization.startswith("Bearer "):
            token = authorization[7:]
            if secrets.compare_digest(token, TASKBOARD_API_KEY):
                return True
    
    # Check X-API-Key header
    if x_api_key:
        if secrets.compare_digest(x_api_key, TASKBOARD_API_KEY):
            return True
    
    raise HTTPException(status_code=401, detail="Invalid or missing API key")

def verify_internal_only(request: Request):
    """Only allow requests from localhost/internal sources."""
    client_host = request.client.host if request.client else None
    allowed_hosts = ["127.0.0.1", "localhost", "::1", "172.17.0.1", "host.docker.internal"]
    
    # Also allow Docker internal IPs (172.x.x.x)
    if client_host and (client_host in allowed_hosts or client_host.startswith("172.")):
        return True
    
    # If API key is provided, allow from anywhere
    if TASKBOARD_API_KEY:
        return True
    
    raise HTTPException(status_code=403, detail="Access denied")

async def notify_MOLTBOT(task_id: int, task_title: str, comment_agent: str, comment_content: str):
    """Send webhook to Clawdbot when a comment needs attention."""
    if not MOLTBOT_ENABLED or comment_agent == MAIN_AGENT_NAME:
        return  # Don't notify for main agent's own comments
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Use MOLTBOT's cron wake endpoint
            payload = {
                "action": "wake",
                "text": f"💬 Task Board: New comment on #{task_id} ({task_title}) from {comment_agent}:\n\n{comment_content[:200]}{'...' if len(comment_content) > 200 else ''}\n\nCheck and respond: http://localhost:8080"
            }
            headers = {
                "Authorization": f"Bearer {MOLTBOT_TOKEN}",
                "Content-Type": "application/json"
            }
            await client.post(f"{MOLTBOT_GATEWAY_URL}/api/cron/wake", json=payload, headers=headers)
            print(f"Notified MOLTBOT about comment from {comment_agent}")
    except Exception as e:
        print(f"Webhook to MOLTBOT failed: {e}")


async def send_to_agent_session(session_key: str, message: str) -> bool:
    """Send a follow-up message to an active agent session."""
    if not MOLTBOT_ENABLED or not session_key:
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
                "Authorization": f"Bearer {MOLTBOT_TOKEN}",
                "Content-Type": "application/json"
            }
            response = await client.post(
                f"{MOLTBOT_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            result = response.json() if response.status_code == 200 else None
            if result and result.get("ok"):
                print(f"✅ Sent message to session {session_key}")
                return True
            else:
                print(f"❌ Failed to send to session: {response.text}")
                return False
    except Exception as e:
        print(f"❌ Failed to send to agent session: {e}")
        return False


def get_task_session(task_id: int) -> Optional[str]:
    """Get the active agent session key for a task."""
    with get_db() as conn:
        row = conn.execute("SELECT agent_session_key FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row["agent_session_key"] if row and row["agent_session_key"] else None


async def spawn_followup_session(task_id: int, task_title: str, agent_name: str, previous_context: str, new_message: str):
    """Spawn a follow-up session for an agent with conversation context."""
    if not MOLTBOT_ENABLED:
        return None
    
    agent_id = AGENT_TO_MOLTBOT_ID.get(agent_name)
    if not agent_id:
        return None
    # Main agent can spawn follow-up sessions too
    
    system_prompt = AGENT_SYSTEM_PROMPTS.get(agent_id, "")
    
    followup_prompt = f"""# Follow-up on Task #{task_id}: {task_title}

You previously worked on this task and moved it to Review. User has a follow-up question.

## Previous Conversation:
{previous_context if previous_context else "(No previous messages)"}

## User's New Message:
{new_message}

## Your Role:
{system_prompt}

## Instructions:
1. Read the context and User's question
2. Respond helpfully by posting a comment: POST http://localhost:8080/api/tasks/{task_id}/comments
3. Keep your response focused on what User asked

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
                "Authorization": f"Bearer {MOLTBOT_TOKEN}",
                "Content-Type": "application/json"
            }
            response = await client.post(
                f"{MOLTBOT_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            result = response.json() if response.status_code == 200 else None
            if result and result.get("ok"):
                spawn_info = result.get("result", {})
                session_key = spawn_info.get("childSessionKey", None)
                if session_key:
                    set_task_session(task_id, session_key)
                print(f"✅ Spawned follow-up session for {agent_name} on task #{task_id}")
                return result
            else:
                print(f"❌ Failed to spawn follow-up: {response.text}")
                return None
    except Exception as e:
        print(f"❌ Failed to spawn follow-up session: {e}")
        return None


def set_task_session(task_id: int, session_key: Optional[str]):
    """Set or clear the agent session key for a task."""
    with get_db() as conn:
        conn.execute(
            "UPDATE tasks SET agent_session_key = ?, updated_at = ? WHERE id = ?",
            (session_key, datetime.now().isoformat(), task_id)
        )
        conn.commit()


async def spawn_mentioned_agent(task_id: int, task_title: str, task_description: str, 
                                 mentioned_agent: str, mentioner: str, comment_content: str,
                                 previous_context: str = ""):
    """Spawn a session for an @mentioned agent to contribute to a task they don't own.
    
    For the main agent (Jarvis), sends to main session instead of spawning.
    """
    if not MOLTBOT_ENABLED:
        return None
    
    agent_id = AGENT_TO_CLAWDBOT_ID.get(mentioned_agent)
    if not agent_id:
        return None
    
    # All agents (including main) now spawn subagent sessions
    system_prompt = AGENT_SYSTEM_PROMPTS.get(agent_id, "")
    
    mention_prompt = f"""# You've Been Tagged: Task #{task_id}

**{mentioner}** mentioned you on a task and needs your input.

## Task: {task_title}
{task_description or '(No description)'}

## What {mentioner} Said:
{comment_content}

## Previous Conversation:
{previous_context if previous_context else "(No prior comments)"}

## Your Role:
{system_prompt}

## Instructions:
1. Call start-work API: POST http://localhost:8080/api/tasks/{task_id}/start-work?agent={mentioned_agent}
2. Review the task from YOUR perspective ({mentioned_agent})
3. Post your findings/response as a comment: POST http://localhost:8080/api/tasks/{task_id}/comments
4. Call stop-work API: POST http://localhost:8080/api/tasks/{task_id}/stop-work

**Note:** You are NOT the assigned owner of this task. You're providing your expertise because you were tagged.
Do NOT move the task to a different status — that's the owner's job.

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
                    "cleanup": "delete"  # Cleanup after since they're just dropping in
                }
            }
            headers = {
                "Authorization": f"Bearer {MOLTBOT_TOKEN}",
                "Content-Type": "application/json"
            }
            response = await client.post(
                f"{MOLTBOT_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            result = response.json() if response.status_code == 200 else None
            if result and result.get("ok"):
                spawn_info = result.get("result", {})
                session_key = spawn_info.get("childSessionKey", "unknown")
                
                # Post system comment about the spawn
                async with httpx.AsyncClient(timeout=5.0) as comment_client:
                    await comment_client.post(
                        f"http://localhost:8080/api/tasks/{task_id}/comments",
                        json={
                            "agent": "System",
                            "content": f"📢 **{mentioned_agent}** was tagged by {mentioner} and is now reviewing this task."
                        }
                    )
                
                print(f"✅ Spawned {mentioned_agent} for mention on task #{task_id}")
                return result
            else:
                print(f"❌ Failed to spawn {mentioned_agent} for mention: {response.text}")
                return None
    except Exception as e:
        print(f"❌ Failed to spawn mentioned agent: {e}")
        return None

# Guardrails to inject into every sub-agent task
AGENT_GUARDRAILS = f"""
⚠️ MANDATORY CONSTRAINTS (Approved by User via Task Board assignment):

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
- Security over convenience — always

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
- Use start-work API when beginning: POST http://localhost:8080/api/tasks/{{task_id}}/start-work?agent={{your_name}}
- Post updates as comments: POST http://localhost:8080/api/tasks/{{task_id}}/comments (json: {{"agent": "your_name", "content": "message"}})
- Create action items for questions: POST http://localhost:8080/api/tasks/{{task_id}}/action-items (json: {{"agent": "your_name", "content": "question", "item_type": "question"}})
- Move to Review when done: POST http://localhost:8080/api/tasks/{{task_id}}/move?status=Review&agent={{your_name}}&reason=...
- Use stop-work API when finished: POST http://localhost:8080/api/tasks/{{task_id}}/stop-work

REPORT FORMAT:
When complete, post a comment with your findings using this format:
## [Your Role] Report
**Task:** [task title]
**Verdict:** ✅ APPROVED / ⚠️ CONCERNS / 🛑 BLOCKED
### Findings
- [SEVERITY] Issue description
### Summary
[1-2 sentence assessment]
"""

AGENT_SYSTEM_PROMPTS = {
    "main": f"""You are {MAIN_AGENT_NAME}, the primary coordinator for {COMPANY_NAME}.

Your focus:
- General task implementation and coordination
- Code writing and debugging
- Cross-cutting concerns that don't fit specialist roles
- Synthesizing input from other agents
- Direct implementation work

Project: {PROJECT_NAME}
You're the hands-on executor. When assigned a task, dig in and get it done.""",

    "architect": f"""You are the Architect for {COMPANY_NAME}.

Your focus:
- System design and architectural patterns
- Scalability and performance implications
- Technical trade-offs and recommendations
- Integration architecture
- Database design and data modeling

Project: {PROJECT_NAME}
Be concise. Flag concerns with severity (CRITICAL/HIGH/MEDIUM/LOW).""",

    "security-auditor": f"""You are the Security Auditor for {COMPANY_NAME}.

Your focus:
- SOC2 Trust Services Criteria (Security, Availability, Confidentiality, Privacy)
- HIPAA compliance (PHI handling, access controls, audit logging)
- CIS Controls benchmarks
- OWASP Top 10 vulnerabilities
- Secure credential storage and handling
- Tenant data isolation (multi-tenant SaaS)

NON-NEGOTIABLE: Security over convenience. Always.
Rate findings: CRITICAL (blocks deploy) / HIGH / MEDIUM / LOW""",

    "code-reviewer": f"""You are the Code Reviewer for {COMPANY_NAME}.

Your focus:
- Code quality and best practices
- DRY, SOLID principles
- Error handling and edge cases
- Performance considerations
- Code readability and maintainability
- Test coverage gaps

Project: {PROJECT_NAME}
Format: MUST FIX / SHOULD FIX / CONSIDER / NICE TO HAVE""",

    "ux-manager": f"""You are the UX Manager for {COMPANY_NAME}.

Your focus:
- User flow clarity and efficiency
- Error message helpfulness
- Form design and validation feedback
- UI consistency across the platform
- Accessibility basics
- Onboarding experience

Project: {PROJECT_NAME}

BROWSER ACCESS (localhost only):
You have browser access to review the app UI. Use it to:
- Take snapshots of pages to analyze layout, spacing, colors
- Check user flows and navigation
- Verify form designs and error states
- Assess overall visual consistency

ALLOWED URLs (localhost only):
- http://localhost:* (any port)
- http://127.0.0.1:*

DO NOT navigate to any external URLs. Your browser access is strictly for reviewing the local app."""
}

async def spawn_agent_session(task_id: int, task_title: str, task_description: str, agent_name: str):
    """Spawn a MOLTBOT sub-agent session for a task via tools/invoke API."""
    if not MOLTBOT_ENABLED:
        return None
    
    agent_id = AGENT_TO_MOLTBOT_ID.get(agent_name)
    if not agent_id:
        return None  # Don't spawn for unknown agents
    # Note: Main agent (Jarvis) CAN spawn subagents now - no special case
    
    # Build the task prompt with guardrails
    system_prompt = AGENT_SYSTEM_PROMPTS.get(agent_id, "")
    task_prompt = f"""# Task Assignment from RIZQ Task Board (Approved by {HUMAN_SUPERVISOR_LABEL})

**Task #{task_id}:** {task_title}

**Description:**
{task_description or 'No description provided.'}

{AGENT_GUARDRAILS}

## Your Role
{system_prompt}

---

## Instructions
1. Call start-work API: POST http://localhost:8080/api/tasks/{task_id}/start-work?agent={agent_name}
2. Analyze the task thoroughly
3. Post your findings as a comment on the task
4. Move to Review when complete: POST http://localhost:8080/api/tasks/{task_id}/move?status=Review&agent={agent_name}&reason=<summary>
5. Call stop-work API: POST http://localhost:8080/api/tasks/{task_id}/stop-work

## IMPORTANT: Stay Available
After posting your findings, **remain available for follow-up questions**. User may reply with questions or requests for clarification. When you receive a message starting with "💬 **User replied**", respond thoughtfully and post your response as a comment on the task.

Your session will automatically end when User marks the task as Done.

Begin now.
"""
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Use MOLTBOT's tools/invoke API to spawn sub-agent directly
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
                "Authorization": f"Bearer {MOLTBOT_TOKEN}",
                "Content-Type": "application/json"
            }
            response = await client.post(
                f"{MOLTBOT_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            result = response.json() if response.status_code == 200 else None
            if result and result.get("ok"):
                print(f"✅ Spawned {agent_name} ({agent_id}) for task #{task_id}")
                # Add a comment to the task noting the agent was spawned
                spawn_info = result.get("result", {})
                run_id = spawn_info.get("runId", "unknown")
                session_key = spawn_info.get("childSessionKey", None)
                
                # Save session key to database for follow-up messages
                if session_key:
                    set_task_session(task_id, session_key)
                
                async with httpx.AsyncClient(timeout=5.0) as comment_client:
                    await comment_client.post(
                        f"http://localhost:8080/api/tasks/{task_id}/comments",
                        json={
                            "agent": "System",
                            "content": f"🤖 **{agent_name}** agent spawned automatically.\n\nSession: `{session_key or 'unknown'}`\nRun ID: `{run_id}`\n\n💬 *Reply to this task and the agent will respond.*"
                        }
                    )
                return result
            else:
                print(f"❌ Failed to spawn {agent_name}: {response.text}")
                return None
    except Exception as e:
        print(f"❌ Failed to spawn agent session: {e}")
        return None

# =============================================================================
# WEBSOCKET MANAGER
# =============================================================================

class ConnectionManager:
    """Manage WebSocket connections for live updates."""
    
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
    
    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)
    
    async def broadcast(self, message: dict):
        """Send update to all connected clients."""
        dead = set()
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                dead.add(connection)
        self.active_connections -= dead

manager = ConnectionManager()

# =============================================================================
# DATABASE
# =============================================================================

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
        # Add working_agent column if it doesn't exist
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN working_agent TEXT DEFAULT NULL")
        except:
            pass  # Column already exists
        # Add agent_session_key column for persistent agent sessions
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN agent_session_key TEXT DEFAULT NULL")
        except:
            pass  # Column already exists
        
        # Add archived column to action_items
        try:
            conn.execute("ALTER TABLE action_items ADD COLUMN archived INTEGER DEFAULT 0")
        except:
            pass  # Column already exists
        
        # Chat messages table for persistent command bar history
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
        # Add session_key column if upgrading from older schema
        try:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN session_key TEXT DEFAULT 'main'")
        except:
            pass  # Column already exists
        
        # Deleted sessions table - to filter out from dropdown
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deleted_sessions (
                session_key TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL
            )
        """)
        conn.commit()

@contextmanager
def get_db():
    """Database connection context manager."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def log_activity(task_id: int, action: str, agent: str = None, details: str = None):
    """Log an activity."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO activity_log (task_id, action, agent, details, timestamp) VALUES (?, ?, ?, ?, ?)",
            (task_id, action, agent, details, datetime.now().isoformat())
        )
        conn.commit()

# =============================================================================
# MODELS
# =============================================================================

class TaskCreate(BaseModel):
    title: str
    description: str = ""
    status: str = "Backlog"
    priority: str = "Medium"
    agent: str = "Unassigned"
    due_date: Optional[str] = None
    board: str = "tasks"
    source_file: Optional[str] = None
    source_ref: Optional[str] = None

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    agent: Optional[str] = None
    due_date: Optional[str] = None
    source_file: Optional[str] = None
    source_ref: Optional[str] = None

class Task(BaseModel):
    id: int
    title: str
    description: str
    status: str
    priority: str
    agent: str
    due_date: Optional[str]
    created_at: str
    updated_at: str
    board: str
    source_file: Optional[str] = None
    source_ref: Optional[str] = None
    working_agent: Optional[str] = None

# =============================================================================
# APP
# =============================================================================

app = FastAPI(title="RIZQ Task Board", version="1.2.0")

# Restrict CORS to localhost origins only
ALLOWED_ORIGINS = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type"],
)

# Initialize DB on startup
@app.on_event("startup")
def startup():
    init_db()

# Serve static files
STATIC_PATH.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_PATH), name="static")

@app.get("/")
def read_root():
    """Serve the Kanban UI."""
    return FileResponse(STATIC_PATH / "index.html")

# =============================================================================
# WEBSOCKET ENDPOINT
# =============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for live updates."""
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive, wait for messages (ping/pong)
            data = await websocket.receive_text()
            # Echo back for ping
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# =============================================================================
# CONFIG ENDPOINTS
# =============================================================================

@app.get("/api/config")
def get_config():
    """Get board configuration including branding."""
    return {
        "agents": AGENTS,
        "statuses": STATUSES,
        "priorities": PRIORITIES,
        "branding": {
            "mainAgentName": MAIN_AGENT_NAME,
            "mainAgentEmoji": MAIN_AGENT_EMOJI,
            "humanName": HUMAN_NAME,
            "humanSupervisorLabel": HUMAN_SUPERVISOR_LABEL,
            "boardTitle": BOARD_TITLE,
        }
    }

# =============================================================================
# TASK ENDPOINTS
# =============================================================================

@app.get("/api/tasks", response_model=List[Task])
def list_tasks(board: str = "tasks", agent: str = None, status: str = None):
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
        
        query += " ORDER BY CASE priority WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 ELSE 4 END, created_at DESC"
        
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

@app.get("/api/tasks/{task_id}", response_model=Task)
def get_task(task_id: int):
    """Get a single task."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        return dict(row)

@app.post("/api/tasks", response_model=Task)
async def create_task(task: TaskCreate):
    """Create a new task."""
    now = datetime.now().isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO tasks (title, description, status, priority, agent, due_date, created_at, updated_at, board, source_file, source_ref)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task.title, task.description, task.status, task.priority, task.agent, task.due_date, now, now, task.board, task.source_file, task.source_ref)
        )
        conn.commit()
        task_id = cursor.lastrowid
        log_activity(task_id, "created", task.agent, f"Created: {task.title}")
        
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = dict(row)
    
    # Broadcast to all clients
    await manager.broadcast({"type": "task_created", "task": result})
    return result

@app.patch("/api/tasks/{task_id}", response_model=Task)
async def update_task(task_id: int, updates: TaskUpdate):
    """Update a task."""
    with get_db() as conn:
        # Get current task
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        
        current = dict(row)
        changes = []
        
        # Build update
        update_fields = []
        params = []
        
        for field in ["title", "description", "status", "priority", "agent", "due_date", "source_file", "source_ref"]:
            new_value = getattr(updates, field)
            if new_value is not None and new_value != current[field]:
                update_fields.append(f"{field} = ?")
                params.append(new_value)
                changes.append(f"{field}: {current[field]} → {new_value}")
        
        if update_fields:
            update_fields.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(task_id)
            
            conn.execute(f"UPDATE tasks SET {', '.join(update_fields)} WHERE id = ?", params)
            conn.commit()
            
            log_activity(task_id, "updated", updates.agent or current["agent"], "; ".join(changes))
        
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = dict(row)
    
    # Broadcast to all clients
    await manager.broadcast({"type": "task_updated", "task": result})
    return result

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    """Delete a task."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        log_activity(task_id, "deleted", None, f"Deleted: {row['title']}")
    
    # Broadcast to all clients
    await manager.broadcast({"type": "task_deleted", "task_id": task_id})
    return {"status": "deleted", "id": task_id}

# =============================================================================
# AGENT ENDPOINTS
# =============================================================================

@app.get("/api/agents/{agent}/tasks")
def get_agent_tasks(agent: str):
    """Get all tasks assigned to an agent."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE agent = ? AND status NOT IN ('Done', 'Blocked') ORDER BY priority, created_at",
            (agent,)
        ).fetchall()
        return [dict(row) for row in rows]

# =============================================================================
# WORK STATUS (AI Activity Indicator)
# =============================================================================

@app.post("/api/tasks/{task_id}/start-work")
async def start_work(task_id: int, agent: str):
    """Mark that an agent is actively working on a task."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        
        conn.execute(
            "UPDATE tasks SET working_agent = ?, updated_at = ? WHERE id = ?",
            (agent, datetime.now().isoformat(), task_id)
        )
        conn.commit()
        
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = dict(row)
    
    await manager.broadcast({"type": "work_started", "task_id": task_id, "agent": agent})
    return {"status": "working", "task_id": task_id, "agent": agent}

@app.post("/api/tasks/{task_id}/stop-work")
async def stop_work(task_id: int, agent: str = None):
    """Mark that an agent has stopped working on a task."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        
        conn.execute(
            "UPDATE tasks SET working_agent = NULL, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), task_id)
        )
        conn.commit()
    
    await manager.broadcast({"type": "work_stopped", "task_id": task_id})
    return {"status": "stopped", "task_id": task_id}

class MoveRequest(BaseModel):
    status: str
    agent: str = None
    reason: str = None  # Required for Review/Blocked transitions

@app.post("/api/tasks/{task_id}/move")
async def move_task(task_id: int, status: str = None, agent: str = None, reason: str = None):
    """Quick move task to a new status with workflow rules."""
    now = datetime.now().isoformat()
    
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        
        task = dict(row)
        old_status = task["status"]
        
        # RULE: Only User (human) can move to Done
        if status == "Done" and agent != "User":
            raise HTTPException(status_code=403, detail="Only User can move tasks to Done")
        
        # Update status
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, task_id)
        )
        conn.commit()
        log_activity(task_id, "moved", agent, f"Moved to {status}")
        
        # AUTO-CREATE ACTION ITEMS based on transition
        action_item = None
        
        # Moving to Review → create completion action item
        if status == "Review" and old_status != "Review":
            content = reason or f"Ready for review: {task['title']}"
            cursor = conn.execute(
                "INSERT INTO action_items (task_id, agent, content, item_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, agent or task["agent"], content, "completion", now)
            )
            conn.commit()
            action_item = {
                "id": cursor.lastrowid, "task_id": task_id, "agent": agent or task["agent"],
                "content": content, "item_type": "completion", "resolved": 0, "created_at": now
            }
        
        # Moving to Blocked → create blocker action item
        if status == "Blocked" and old_status != "Blocked":
            content = reason or f"Blocked: {task['title']} - reason not specified"
            cursor = conn.execute(
                "INSERT INTO action_items (task_id, agent, content, item_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, agent or task["agent"], content, "blocker", now)
            )
            conn.commit()
            action_item = {
                "id": cursor.lastrowid, "task_id": task_id, "agent": agent or task["agent"],
                "content": content, "item_type": "blocker", "resolved": 0, "created_at": now
            }
        
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = dict(row)
    
    # Broadcast updates
    await manager.broadcast({"type": "task_updated", "task": result})
    if action_item:
        await manager.broadcast({"type": "action_item_added", "task_id": task_id, "item": action_item})
    
    # AUTO-SPAWN: When moving to In Progress, spawn the assigned agent's session
    spawned = False
    if status == "In Progress" and old_status != "In Progress":
        assigned_agent = result.get("agent", "Unassigned")
        if assigned_agent in AGENT_TO_MOLTBOT_ID and assigned_agent != "User":
            await spawn_agent_session(
                task_id=task_id,
                task_title=result["title"],
                task_description=result.get("description", ""),
                agent_name=assigned_agent
            )
            spawned = True
    
    # CLEANUP: When moving to Done, clear the agent session AND working indicator
    session_cleared = False
    if status == "Done":
        # Always clear working_agent when task is Done
        with get_db() as conn:
            conn.execute(
                "UPDATE tasks SET working_agent = NULL WHERE id = ?",
                (task_id,)
            )
            conn.commit()
        await manager.broadcast({"type": "work_stopped", "task_id": task_id})
        
        session_key = get_task_session(task_id)
        if session_key:
            # Notify the agent that the task is complete
            await send_to_agent_session(session_key, 
                f"✅ **Task #{task_id} marked as Done by User.**\n\nYour work is complete. This session will now end. Thank you!")
            # Clear the session from the database
            set_task_session(task_id, None)
            session_cleared = True
            print(f"🧹 Cleared agent session for task #{task_id}")
    
    return {"status": "moved", "new_status": status, "action_item_created": action_item is not None, "agent_spawned": spawned, "session_cleared": session_cleared}

# =============================================================================
# COMMENTS
# =============================================================================

class CommentCreate(BaseModel):
    agent: str
    content: str
    
    @field_validator('content')
    @classmethod
    def validate_content_size(cls, v):
        # Limit content to 10MB (base64 images can be large)
        if len(v) > MAX_ATTACHMENT_SIZE_BYTES:
            raise ValueError(f'Content exceeds maximum size of {MAX_ATTACHMENT_SIZE_MB}MB')
        return v
    
    @field_validator('agent')
    @classmethod
    def validate_agent(cls, v):
        if len(v) > 100:
            raise ValueError('Agent name too long')
        return v

@app.get("/api/tasks/{task_id}/comments")
def get_comments(task_id: int):
    """Get comments for a task."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM comments WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,)
        ).fetchall()
        return [dict(row) for row in rows]

@app.post("/api/tasks/{task_id}/comments")
async def add_comment(task_id: int, comment: CommentCreate):
    """Add a comment to a task."""
    now = datetime.now().isoformat()
    task_title = ""
    task_status = ""
    agent_session = None
    
    with get_db() as conn:
        # Verify task exists
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
        conn.commit()
        
        result = {
            "id": cursor.lastrowid,
            "task_id": task_id,
            "agent": comment.agent,
            "content": comment.content,
            "created_at": now
        }
    
    # Broadcast to all clients
    await manager.broadcast({"type": "comment_added", "task_id": task_id, "comment": result})
    
    # Check for @mentions in the comment and spawn mentioned agents
    mentions = MENTION_PATTERN.findall(comment.content)
    if mentions:
        # Get task description and previous context for the spawned agent
        task_description = ""
        previous_context = ""
        with get_db() as conn:
            task_row = conn.execute("SELECT description FROM tasks WHERE id = ?", (task_id,)).fetchone()
            task_description = task_row["description"] if task_row else ""
            
            # Get last few comments for context (excluding the one that just triggered this)
            comment_rows = conn.execute(
                "SELECT agent, content FROM comments WHERE task_id = ? AND id != ? ORDER BY created_at DESC LIMIT 5",
                (task_id, result["id"])
            ).fetchall()
            if comment_rows:
                previous_context = "\n".join([f"**{r['agent']}:** {r['content'][:500]}" for r in reversed(comment_rows)])
        
        for mentioned_agent in set(mentions):  # dedupe mentions
            # Normalize case to match AGENT_TO_CLAWDBOT_ID keys
            matched_agent = None
            for agent_name in AGENT_TO_CLAWDBOT_ID.keys():
                if agent_name.lower() == mentioned_agent.lower():
                    matched_agent = agent_name
                    break
            
            if matched_agent and matched_agent != comment.agent:  # Don't spawn self
                agent_id = AGENT_TO_CLAWDBOT_ID.get(matched_agent)
                if agent_id:  # All agents including main can be spawned now
                    # Spawn the mentioned agent to respond
                    await spawn_mentioned_agent(
                        task_id=task_id,
                        task_title=task_title,
                        task_description=task_description,
                        mentioned_agent=matched_agent,
                        mentioner=comment.agent,
                        comment_content=comment.content,
                        previous_context=previous_context
                    )
                    print(f"📢 Spawned {matched_agent} for mention in task #{task_id}")
    
    # If this is from User and task is active, try to reach the agent
    if comment.agent == "User" and task_status in ["In Progress", "Review"]:
        # Get the assigned agent for this task
        with get_db() as conn:
            row = conn.execute("SELECT agent FROM tasks WHERE id = ?", (task_id,)).fetchone()
            assigned_agent = row["agent"] if row else None
        
        if assigned_agent and assigned_agent in AGENT_TO_MOLTBOT_ID and assigned_agent != "User":
            # Get previous conversation context (last few comments)
            previous_comments = []
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT agent, content FROM comments WHERE task_id = ? ORDER BY created_at DESC LIMIT 5",
                    (task_id,)
                ).fetchall()
                previous_comments = [{"agent": r["agent"], "content": r["content"][:500]} for r in reversed(rows)]
            
            context = "\n".join([f"**{c['agent']}:** {c['content']}" for c in previous_comments[:-1]])  # Exclude current comment
            
            # Try to send to existing session first
            sent = False
            if agent_session:
                message = f"""💬 **User replied on Task #{task_id}:**

{comment.content}

---
Respond by posting a comment to the task."""
                sent = await send_to_agent_session(agent_session, message)
            
            if not sent:
                # Session ended - spawn a new one with context
                print(f"🔄 Session ended, spawning follow-up for task #{task_id}")
                await spawn_followup_session(
                    task_id=task_id,
                    task_title=task_title,
                    agent_name=assigned_agent,
                    previous_context=context,
                    new_message=comment.content
                )
    elif comment.agent not in ["System", "User"] + list(AGENT_TO_MOLTBOT_ID.keys()):
        # Notify MOLTBOT for other comments
        await notify_MOLTBOT(task_id, task_title, comment.agent, comment.content)
    
    return result

# =============================================================================
# ACTION ITEMS (Questions, Notifications, Blockers)
# =============================================================================

class ActionItemCreate(BaseModel):
    agent: str
    content: str
    item_type: str = "question"  # question, completion, blocker
    comment_id: Optional[int] = None

@app.get("/api/tasks/{task_id}/action-items")
def get_action_items(task_id: int, resolved: bool = False, archived: bool = False):
    """Get action items for a task. By default excludes archived items."""
    with get_db() as conn:
        if archived:
            # Only return archived items
            rows = conn.execute(
                "SELECT * FROM action_items WHERE task_id = ? AND archived = 1 ORDER BY created_at ASC",
                (task_id,)
            ).fetchall()
        else:
            # Return non-archived items filtered by resolved status
            rows = conn.execute(
                "SELECT * FROM action_items WHERE task_id = ? AND resolved = ? AND archived = 0 ORDER BY created_at ASC",
                (task_id, 1 if resolved else 0)
            ).fetchall()
        return [dict(row) for row in rows]

@app.post("/api/tasks/{task_id}/action-items")
async def add_action_item(task_id: int, item: ActionItemCreate):
    """Add an action item to a task."""
    now = datetime.now().isoformat()
    with get_db() as conn:
        # Verify task exists
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        
        cursor = conn.execute(
            "INSERT INTO action_items (task_id, comment_id, agent, content, item_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, item.comment_id, item.agent, item.content, item.item_type, now)
        )
        conn.commit()
        
        result = {
            "id": cursor.lastrowid,
            "task_id": task_id,
            "comment_id": item.comment_id,
            "agent": item.agent,
            "content": item.content,
            "item_type": item.item_type,
            "resolved": 0,
            "created_at": now,
            "resolved_at": None
        }
    
    # Broadcast to all clients
    await manager.broadcast({"type": "action_item_added", "task_id": task_id, "item": result})
    
    return result

@app.post("/api/action-items/{item_id}/resolve")
async def resolve_action_item(item_id: int):
    """Resolve an action item."""
    now = datetime.now().isoformat()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM action_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action item not found")
        
        conn.execute(
            "UPDATE action_items SET resolved = 1, resolved_at = ? WHERE id = ?",
            (now, item_id)
        )
        conn.commit()
        
        task_id = row["task_id"]
    
    # Broadcast to all clients
    await manager.broadcast({"type": "action_item_resolved", "task_id": task_id, "item_id": item_id})
    
    return {"success": True, "item_id": item_id}


@app.post("/api/action-items/{item_id}/archive")
async def archive_action_item(item_id: int):
    """Archive a resolved action item to hide it from main view."""
    now = datetime.now().isoformat()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM action_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action item not found")
        
        conn.execute(
            "UPDATE action_items SET archived = 1 WHERE id = ?",
            (item_id,)
        )
        conn.commit()
        
        task_id = row["task_id"]
    
    # Broadcast to all clients
    await manager.broadcast({"type": "action_item_archived", "task_id": task_id, "item_id": item_id})
    
    return {"success": True, "item_id": item_id}


@app.post("/api/action-items/{item_id}/unarchive")
async def unarchive_action_item(item_id: int):
    """Unarchive an action item to show it in main view again."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM action_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action item not found")
        
        conn.execute(
            "UPDATE action_items SET archived = 0 WHERE id = ?",
            (item_id,)
        )
        conn.commit()
        
        task_id = row["task_id"]
    
    # Broadcast to all clients
    await manager.broadcast({"type": "action_item_unarchived", "task_id": task_id, "item_id": item_id})
    
    return {"success": True, "item_id": item_id}

@app.delete("/api/action-items/{item_id}")
async def delete_action_item(item_id: int):
    """Delete an action item."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM action_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action item not found")
        
        task_id = row["task_id"]
        conn.execute("DELETE FROM action_items WHERE id = ?", (item_id,))
        conn.commit()
    
    # Broadcast to all clients
    await manager.broadcast({"type": "action_item_deleted", "task_id": task_id, "item_id": item_id})
    
    return {"success": True, "item_id": item_id}

# =============================================================================
# ACTIVITY LOG
# =============================================================================

@app.get("/api/activity")
def get_activity(limit: int = 50):
    """Get recent activity."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


# =============================================================================
# JARVIS DIRECT CHAT (Command Bar Channel)
# =============================================================================

class JarvisMessage(BaseModel):
    message: str
    session: str = "main"  # Which session to send to
    attachments: Optional[List[dict]] = None  # [{type: "image/png", data: "base64...", filename: "..."}]
    
    @field_validator('message')
    @classmethod
    def validate_message_size(cls, v):
        if len(v) > MAX_ATTACHMENT_SIZE_BYTES:
            raise ValueError(f'Message exceeds maximum size of {MAX_ATTACHMENT_SIZE_MB}MB')
        return v

# Chat history now persisted in SQLite (no more in-memory loss on refresh)

# =============================================================================
# CLAWDBOT SESSIONS API
# =============================================================================

@app.get("/api/sessions")
async def list_sessions():
    """Proxy to Clawdbot sessions_list to get active sessions."""
    if not MOLTBOT_ENABLED:
        return {"sessions": [], "error": "Clawdbot integration not enabled"}
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "tool": "sessions_list",
                "args": {
                    "limit": 20,
                    "messageLimit": 0
                }
            }
            headers = {
                "Authorization": f"Bearer {MOLTBOT_TOKEN}",
                "Content-Type": "application/json"
            }
            response = await client.post(
                f"{MOLTBOT_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("ok"):
                    # Response is in result.content[0].text as JSON string
                    inner_result = result.get("result", {})
                    content = inner_result.get("content", [])
                    if content and len(content) > 0:
                        text_content = content[0].get("text", "{}")
                        sessions_data = json.loads(text_content)
                    else:
                        sessions_data = inner_result
                    sessions = sessions_data.get("sessions", [])
                    
                    # Format for frontend
                    formatted = []
                    for s in sessions:
                        key = s.get("key", "")
                        session_label = s.get("label", "")  # Label from Clawdbot
                        display = s.get("displayName", key)
                        
                        # Use Clawdbot's label
                        if key == "main" or key == "agent:main:main":
                            label = "🛡️ Jarvis (Main)"
                        elif session_label:
                            # Use Clawdbot's label if available
                            label = f"🤖 {session_label}"
                        elif "subagent" in key:
                            # Subagent without label - use short ID
                            short_id = key.split(":")[-1][:8] if ":" in key else key[:8]
                            label = f"🤖 Session {short_id}"
                        elif key.startswith("agent:"):
                            parts = key.split(":")
                            agent_name = parts[1] if len(parts) > 1 else key
                            label = f"🤖 {agent_name.title()}"
                        else:
                            label = display
                        
                        formatted.append({
                            "key": key,
                            "label": label,
                            "channel": s.get("channel", ""),
                            "model": s.get("model", ""),
                            "updatedAt": s.get("updatedAt", 0)
                        })
                    
                    # Filter out deleted sessions and cleanup stale entries
                    clawdbot_keys = set(s["key"] for s in formatted)
                    
                    with get_db() as conn:
                        deleted_rows = conn.execute("SELECT session_key FROM deleted_sessions").fetchall()
                        deleted_keys = set(row["session_key"] for row in deleted_rows)
                        
                        # Cleanup: remove deleted_sessions entries that are no longer in Clawdbot
                        # (Clawdbot has already removed them, so we don't need to track them anymore)
                        orphaned_keys = deleted_keys - clawdbot_keys
                        if orphaned_keys:
                            placeholders = ",".join("?" * len(orphaned_keys))
                            conn.execute(f"DELETE FROM deleted_sessions WHERE session_key IN ({placeholders})", 
                                        list(orphaned_keys))
                            conn.commit()
                    
                    formatted = [s for s in formatted if s["key"] not in deleted_keys]
                    
                    # Sort: main first, then by updatedAt
                    formatted.sort(key=lambda x: (0 if "main" in x["key"].lower() else 1, -x.get("updatedAt", 0)))
                    return {"sessions": formatted}
            
            return {"sessions": [], "error": f"Failed to fetch sessions: {response.status_code}"}
    except Exception as e:
        print(f"Error fetching sessions: {e}")
        return {"sessions": [], "error": str(e)}


class SessionCreate(BaseModel):
    label: str = None
    agentId: str = "main"
    task: str = "New session started from Task Board. Awaiting instructions."


@app.post("/api/sessions/create")
async def create_session(req: SessionCreate):
    """Create a new Clawdbot session via sessions_spawn."""
    if not MOLTBOT_ENABLED:
        return {"success": False, "error": "Clawdbot integration not enabled"}
    
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
            headers = {
                "Authorization": f"Bearer {MOLTBOT_TOKEN}",
                "Content-Type": "application/json"
            }
            response = await client.post(
                f"{MOLTBOT_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("ok"):
                    return {"success": True, "result": result.get("result", {})}
            
            return {"success": False, "error": f"Failed: {response.status_code}"}
    except Exception as e:
        print(f"Error creating session: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/sessions/{session_key}/stop")
async def stop_session(session_key: str):
    """Stop/abort a running session."""
    if not MOLTBOT_ENABLED:
        return {"success": False, "error": "Clawdbot integration not enabled"}
    
    try:
        # Use the gateway's abort mechanism
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Send an abort signal via sessions_send with a special abort message
            payload = {
                "tool": "sessions_send",
                "args": {
                    "sessionKey": session_key,
                    "message": "SYSTEM: ABORT - User requested stop from Task Board"
                }
            }
            headers = {
                "Authorization": f"Bearer {MOLTBOT_TOKEN}",
                "Content-Type": "application/json"
            }
            
            # First try to send abort message
            await client.post(
                f"{MOLTBOT_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            
            # Also try the direct abort endpoint if available
            try:
                abort_response = await client.post(
                    f"{MOLTBOT_GATEWAY_URL}/api/sessions/{session_key}/abort",
                    headers=headers
                )
                if abort_response.status_code == 200:
                    return {"success": True, "message": f"Stopped session: {session_key}"}
            except:
                pass
            
            return {"success": True, "message": f"Stop signal sent to: {session_key}"}
    except Exception as e:
        print(f"Error stopping session: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/sessions/stop-all")
async def stop_all_sessions():
    """Emergency stop all non-main sessions."""
    if not MOLTBOT_ENABLED:
        return {"success": False, "error": "Clawdbot integration not enabled"}
    
    stopped = []
    errors = []
    
    try:
        # First get all sessions
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "tool": "sessions_list",
                "args": {"limit": 50, "messageLimit": 0}
            }
            headers = {
                "Authorization": f"Bearer {MOLTBOT_TOKEN}",
                "Content-Type": "application/json"
            }
            response = await client.post(
                f"{MOLTBOT_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            
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
                    
                    # Stop each non-main session
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
        
        return {
            "success": True,
            "stopped": stopped,
            "errors": errors,
            "message": f"Stopped {len(stopped)} sessions"
        }
    except Exception as e:
        print(f"Error stopping all sessions: {e}")
        return {"success": False, "error": str(e)}


@app.delete("/api/sessions/{session_key}")
async def delete_session(session_key: str):
    """Close/delete a session - removes from Clawdbot's session store."""
    if not MOLTBOT_ENABLED:
        return {"success": False, "error": "Clawdbot integration not enabled"}
    
    # Send stop signal first
    await stop_session(session_key)
    
    now = datetime.now().isoformat()
    
    # Clear taskboard's local chat history
    with get_db() as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_key = ?", (session_key,))
        conn.execute(
            "INSERT OR REPLACE INTO deleted_sessions (session_key, deleted_at) VALUES (?, ?)",
            (session_key, now)
        )
        conn.commit()
    
    # Delete from Clawdbot's session store
    clawdbot_deleted = False
    try:
        # Parse session key to get agent id (format: agent:<agentId>:<rest>)
        parts = session_key.split(":")
        if len(parts) >= 2 and parts[0] == "agent":
            agent_id = parts[1]  # e.g., "main"
            
            # Path to Clawdbot session store (use env var if in Docker, fallback to home dir)
            import os
            clawdbot_home = os.environ.get("CLAWDBOT_DATA_PATH", os.path.expanduser("~/.clawdbot"))
            sessions_file = os.path.join(clawdbot_home, "agents", agent_id, "sessions", "sessions.json")
            
            if os.path.exists(sessions_file):
                import json
                with open(sessions_file, 'r', encoding='utf-8') as f:
                    sessions_data = json.load(f)
                
                # Check if session exists and get its sessionId for transcript deletion
                session_id = None
                if session_key in sessions_data:
                    session_id = sessions_data[session_key].get("sessionId")
                    del sessions_data[session_key]
                    
                    # Write back
                    with open(sessions_file, 'w', encoding='utf-8') as f:
                        json.dump(sessions_data, f, indent=2)
                    
                    clawdbot_deleted = True
                    print(f"Deleted session {session_key} from Clawdbot store")
                    
                    # Also delete transcript file if it exists
                    if session_id:
                        transcript_file = os.path.join(clawdbot_home, "agents", agent_id, "sessions", f"{session_id}.jsonl")
                        if os.path.exists(transcript_file):
                            os.remove(transcript_file)
                            print(f"Deleted transcript {transcript_file}")
    except Exception as e:
        print(f"Warning: Could not delete from Clawdbot store: {e}")
    
    # Broadcast session deletion to all clients for real-time UI update
    await manager.broadcast({
        "type": "session_deleted",
        "session_key": session_key
    })
    
    return {
        "success": True, 
        "message": f"Deleted session: {session_key}",
        "clawdbot_deleted": clawdbot_deleted
    }


@app.get("/api/jarvis/history")
def get_chat_history(limit: int = 100, session: str = "main"):
    """Get command bar chat history from database, filtered by session."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, session_key, role, content, attachments, created_at FROM chat_messages WHERE session_key = ? ORDER BY id DESC LIMIT ?",
            (session, limit)
        ).fetchall()
        # Return in chronological order
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

@app.post("/api/jarvis/chat")
async def chat_with_jarvis(msg: JarvisMessage):
    """Send a message to Jarvis via sessions_send (synchronous, waits for response)."""
    if not MOLTBOT_ENABLED:
        return {"sent": False, "error": "Clawdbot integration not enabled."}
    
    now = datetime.now().isoformat()
    
    # Build the message content with taskboard context
    message_content = f"System: [TASKBOARD_CHAT] User says: {msg.message}\n\nRespond naturally."
    
    # Include attachment data in the message for the agent to process
    if msg.attachments:
        for att in msg.attachments:
            att_type = att.get("type", "")
            att_data = att.get("data", "")
            att_filename = att.get("filename", "file")
            
            if att_type.startswith("image/") and att_data:
                # Embed full base64 image data so agent can use image tool
                message_content += f"\n\n[IMAGE:{att_data}]"
            elif att_data:
                # For text files, try to extract and embed the content
                if att_data.startswith("data:") and ";base64," in att_data:
                    try:
                        import base64
                        # Extract base64 part after the comma
                        b64_content = att_data.split(",", 1)[1]
                        decoded = base64.b64decode(b64_content).decode("utf-8", errors="replace")
                        message_content += f"\n\n**📎 Attached file: {att_filename}**\n```\n{decoded}\n```"
                    except Exception as e:
                        message_content += f"\n\n[Attached File: {att_filename} (decode error: {e})]"
                else:
                    message_content += f"\n\n[Attached File: {att_filename}]"
    
    # Normalize session key
    session_key = msg.session or "main"
    
    # Store user message in database
    attachments_json = json.dumps(msg.attachments) if msg.attachments else None
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO chat_messages (session_key, role, content, attachments, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_key, "user", msg.message, attachments_json, now)
        )
        conn.commit()
        user_msg_id = cursor.lastrowid
    
    user_msg = {
        "id": user_msg_id,
        "session_key": session_key,
        "role": "user",
        "content": msg.message,
        "timestamp": now,
        "attachments": msg.attachments
    }
    
    # Broadcast user message to all clients (so other tabs see it)
    await manager.broadcast({
        "type": "command_bar_message",
        "message": user_msg
    })
    
    try:
        # Use sessions_send via tools/invoke - this is synchronous and waits for response
        async with httpx.AsyncClient(timeout=120.0) as client:
            payload = {
                "tool": "sessions_send",
                "args": {
                    "message": message_content,
                    "sessionKey": session_key,  # Use selected session
                    "timeoutSeconds": 90
                }
            }
            headers = {
                "Authorization": f"Bearer {MOLTBOT_TOKEN}",
                "Content-Type": "application/json"
            }
            
            response = await client.post(
                f"{MOLTBOT_GATEWAY_URL}/tools/invoke",
                json=payload,
                headers=headers
            )
            
            if response.status_code == 200:
                result = response.json()
                
                # /tools/invoke returns { ok: true, result: { content, details: { reply, ... } } }
                inner = result.get("result", {})
                
                if isinstance(inner, dict):
                    # Response is in inner.details.reply
                    details = inner.get("details", {})
                    assistant_reply = details.get("reply") or inner.get("reply") or inner.get("response")
                else:
                    assistant_reply = str(inner) if inner else None
                
                # Ensure it's a string
                if assistant_reply and not isinstance(assistant_reply, str):
                    import json as json_module
                    assistant_reply = json_module.dumps(assistant_reply) if isinstance(assistant_reply, (dict, list)) else str(assistant_reply)
                
                if assistant_reply:
                    # Store the response in database
                    with get_db() as conn:
                        cursor = conn.execute(
                            "INSERT INTO chat_messages (session_key, role, content, attachments, created_at) VALUES (?, ?, ?, ?, ?)",
                            (session_key, "assistant", assistant_reply, None, now)
                        )
                        conn.commit()
                        assistant_msg_id = cursor.lastrowid
                    
                    jarvis_msg = {
                        "id": assistant_msg_id,
                        "session_key": session_key,
                        "role": "assistant", 
                        "content": assistant_reply,
                        "timestamp": datetime.now().isoformat()
                    }
                    # Return response directly - frontend adds to history from HTTP response
                    return {"sent": True, "response": assistant_reply, "session": session_key}
                
                return {"sent": True, "response": "No response received"}
            else:
                error_text = response.text[:200] if response.text else f"HTTP {response.status_code}"
                return {"sent": False, "error": error_text}
                
    except Exception as e:
        print(f"Error sending to Jarvis: {e}")
        return {"sent": False, "error": str(e)}

class JarvisResponse(BaseModel):
    response: str
    session: str = "main"  # Which session this response is for
    
    @field_validator('response')
    @classmethod
    def validate_response_size(cls, v):
        if len(v) > 1024 * 1024:  # 1MB limit for responses
            raise ValueError('Response too large')
        return v

@app.post("/api/jarvis/respond")
async def jarvis_respond(msg: JarvisResponse, _: bool = Depends(verify_api_key)):
    """Endpoint for Jarvis to push responses back to the command bar. Requires API key."""
    now = datetime.now().isoformat()
    session_key = msg.session or "main"
    
    # Store Jarvis response in database
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO chat_messages (session_key, role, content, attachments, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_key, "assistant", msg.response, None, now)
        )
        conn.commit()
        msg_id = cursor.lastrowid
    
    jarvis_msg = {
        "id": msg_id,
        "session_key": session_key,
        "role": "assistant",
        "content": msg.response,
        "timestamp": now
    }
    
    # Broadcast to all connected clients
    await manager.broadcast({
        "type": "command_bar_message",
        "message": jarvis_msg
    })
    return {"delivered": True}

# Legacy endpoint for backwards compatibility
@app.post("/api/molt/chat")
async def chat_with_molt_legacy(msg: JarvisMessage):
    """Legacy endpoint - redirects to /api/jarvis/chat."""
    return await chat_with_jarvis(msg)

@app.post("/api/molt/respond")
async def jarvis_respond_legacy(msg: JarvisResponse, _: bool = Depends(verify_api_key)):
    """Legacy endpoint - redirects to /api/jarvis/respond."""
    return await jarvis_respond(msg, _)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
