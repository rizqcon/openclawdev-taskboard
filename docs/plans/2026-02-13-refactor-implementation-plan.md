# OpenDevBoard Refactor & Feature Expansion — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refactor monolithic app.py into a clean FastAPI package, add multi-project support, Todo column, advanced filtering, responsive design, markdown export, and harden agent lifecycle.

**Architecture:** Split app.py into `app/` package with APIRouters per domain. Add `projects` table linked to tasks. All frontend changes in single `static/index.html`. Client-side filtering, server-side validation.

**Tech Stack:** FastAPI, SQLite, Pydantic, Vanilla JS/CSS, WebSockets, OpenClaw API

**User Workflow:** After each major task (marked with CHECKPOINT), pause for user review and commit.

---

## Phase 1: Backend Refactoring — Split app.py into Package

### Task 1: Create package skeleton and config module

**Files:**
- Create: `app/__init__.py`
- Create: `app/config.py`

**Step 1: Create `app/__init__.py`**

Empty file to make `app/` a Python package:

```python
# app/__init__.py
```

**Step 2: Create `app/config.py`**

Extract all env-var reading and configuration constants from `app.py` into a config module. This includes:
- `OPENCLAW_GATEWAY_URL`, `OPENCLAW_TOKEN`, `TASKBOARD_API_KEY`, `TASKBOARD_BASE_URL`
- `ALLOWED_IPS`, `ALLOWED_PATHS`
- Agent/branding config: `MAIN_AGENT_NAME`, `MAIN_AGENT_EMOJI`, `HUMAN_NAME`, etc.
- `PROJECT_NAME`, `COMPANY_NAME`, `COMPANY_CONTEXT`, `COMPLIANCE_FRAMEWORKS`
- Valid statuses list: `VALID_STATUSES = ["Backlog", "Todo", "In Progress", "Review", "Done", "Blocked"]`
- Valid priorities list: `VALID_PRIORITIES = ["Critical", "High", "Medium", "Low"]`

Reference: Read top ~100 lines of `app.py` for all `os.getenv()` calls and constants.

---

### Task 2: Create database module

**Files:**
- Create: `app/database.py`

**Step 1: Extract database code from `app.py`**

Move these from `app.py` into `app/database.py`:
- `get_db()` function (SQLite connection factory with WAL mode)
- `init_db()` function (CREATE TABLE statements)
- Any migration/upgrade logic

The `init_db()` function should be importable and called during app startup.

Reference: Search `app.py` for `sqlite3`, `CREATE TABLE`, `get_db`.

---

### Task 3: Create models module

**Files:**
- Create: `app/models.py`

**Step 1: Extract all Pydantic models from `app.py`**

Move these classes:
- `TaskCreate`, `TaskUpdate`
- `CommentCreate`
- `ActionItemCreate`
- `JarvisMessage`
- Any other Pydantic BaseModel subclasses

Add status validation to `TaskCreate` and `TaskUpdate`:
```python
from app.config import VALID_STATUSES, VALID_PRIORITIES

class TaskCreate(BaseModel):
    # ... existing fields ...
    status: str = "Backlog"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{v}'. Must be one of: {VALID_STATUSES}")
        return v

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v):
        if v and v not in VALID_PRIORITIES:
            raise ValueError(f"Invalid priority '{v}'. Must be one of: {VALID_PRIORITIES}")
        return v
```

Same validators on `TaskUpdate` (but fields are Optional).

---

### Task 4: Create WebSocket manager module

**Files:**
- Create: `app/websocket.py`

**Step 1: Extract WebSocket manager from `app.py`**

Move the `ConnectionManager` class and the `broadcast()` helper function. This module should be importable by all route modules.

Reference: Search `app.py` for `class ConnectionManager`, `websocket`, `broadcast`.

---

### Task 5: Create OpenClaw integration module

**Files:**
- Create: `app/openclaw.py`

**Step 1: Extract OpenClaw helper functions from `app.py`**

Move these functions:
- `spawn_agent_session()`
- `send_to_agent_session()`
- `get_agent_system_prompt()` (or similar prompt builder)
- Any `httpx` / `aiohttp` calls to OpenClaw gateway
- Session management helpers

Reference: Search `app.py` for `openclaw`, `spawn`, `tools/invoke`, `sessions_spawn`.

---

### Task 6: Create route modules

**Files:**
- Create: `app/routes/__init__.py`
- Create: `app/routes/tasks.py`
- Create: `app/routes/comments.py`
- Create: `app/routes/action_items.py`
- Create: `app/routes/sessions.py`
- Create: `app/routes/chat.py`
- Create: `app/routes/uploads.py`
- Create: `app/routes/projects.py` (empty placeholder for now)

**Step 1: Create `app/routes/__init__.py`**

Collects all routers:
```python
from fastapi import APIRouter
from app.routes.tasks import router as tasks_router
from app.routes.comments import router as comments_router
from app.routes.action_items import router as action_items_router
from app.routes.sessions import router as sessions_router
from app.routes.chat import router as chat_router
from app.routes.uploads import router as uploads_router
from app.routes.projects import router as projects_router

api_router = APIRouter()
api_router.include_router(tasks_router)
api_router.include_router(comments_router)
api_router.include_router(action_items_router)
api_router.include_router(sessions_router)
api_router.include_router(chat_router)
api_router.include_router(uploads_router)
api_router.include_router(projects_router)
```

**Step 2: Split endpoints into route files**

Each route file follows this pattern:
```python
from fastapi import APIRouter, Depends, HTTPException
from app.database import get_db
from app.models import ...
from app.websocket import manager, broadcast
from app.config import ...

router = APIRouter()

@router.get("/api/tasks")
async def get_tasks(...):
    ...
```

Split by domain:
- `tasks.py`: `/api/tasks` CRUD, `/api/tasks/{id}/move`, `/api/tasks/{id}/start-work`, `/api/tasks/{id}/stop-work`, `/api/agents/{agent}/tasks`, `/api/config`, `/api/activity`
- `comments.py`: `/api/tasks/{id}/comments` GET/POST/DELETE
- `action_items.py`: `/api/tasks/{id}/action-items` GET/POST, `/api/action-items/{id}/resolve|unresolve|archive|unarchive`, DELETE
- `sessions.py`: `/api/sessions` GET, `/api/sessions/create`, `/api/sessions/{key}/stop`, `/api/sessions/stop-all`, DELETE
- `chat.py`: `/api/jarvis/history`, `/api/jarvis/chat`, `/api/jarvis/respond`
- `uploads.py`: `/api/upload/image`
- `projects.py`: Empty router placeholder (implemented in Phase 2)

---

### Task 7: Create main.py entry point

**Files:**
- Create: `app/main.py`

**Step 1: Create the app factory and entry point**

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.cors import CORSMiddleware

from app.config import ALLOWED_IPS, ...
from app.database import init_db
from app.websocket import manager
from app.routes import api_router

app = FastAPI(title="OpenDevBoard")

# Middleware (move from app.py)
# - IPRestrictionMiddleware
# - RequestLoggingMiddleware
# - CORSMiddleware

# Include all routes
app.include_router(api_router)

# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket):
    # ... existing WS logic ...

# Serve static files
@app.get("/")
async def root():
    return FileResponse("static/index.html")

# Startup event
@app.on_event("startup")
async def startup():
    init_db()
```

Move middleware classes (IPRestrictionMiddleware, RequestLoggingMiddleware) either inline in main.py or into a separate `app/middleware.py` if they're large.

---

### Task 8: Verify refactored backend works

**Step 1: Update imports and fix any circular dependencies**

Run the app and verify all endpoints work:
```bash
cd /home/matthias/home-stack/openclawdev-taskboard
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

**Step 2: Test key endpoints manually**
- `GET /` → serves index.html
- `GET /api/config` → returns config
- `GET /api/tasks` → returns tasks
- WebSocket connection at `/ws` → connects

**Step 3: Verify WebSocket live updates still function**
- Open browser, create/move a task, confirm real-time updates

### **CHECKPOINT 1** — Pause for user review and commit. Backend refactoring complete, app runs identically to before.

---

## Phase 2: Database Migration & Multi-Project Support

### Task 9: Add projects table and migrate tasks

**Files:**
- Modify: `app/database.py` — add projects table creation, ALTER tasks table
- Modify: `app/models.py` — add ProjectCreate, ProjectResponse models

**Step 1: Update `init_db()` in `app/database.py`**

Add after existing CREATE TABLE statements:
```python
# Projects table
cursor.execute("""
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
    cursor.execute("ALTER TABLE tasks ADD COLUMN project_id INTEGER DEFAULT 1 REFERENCES projects(id)")
except Exception:
    pass  # Column already exists

# Ensure default project exists
cursor.execute("SELECT id FROM projects WHERE slug = 'default'")
if not cursor.fetchone():
    cursor.execute(
        "INSERT INTO projects (name, slug, description, color, created_at) VALUES (?, ?, ?, ?, ?)",
        ("Default", "default", "Default project", "#00b4d8", datetime.now(timezone.utc).isoformat())
    )

conn.commit()
```

**Step 2: Add Pydantic models in `app/models.py`**

```python
class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    color: str = Field(default="#00b4d8", pattern=r"^#[0-9a-fA-F]{6}$")

class ProjectResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: str
    color: str
    created_at: str
```

---

### Task 10: Implement project API endpoints

**Files:**
- Modify: `app/routes/projects.py`

**Step 1: Implement CRUD endpoints**

```python
router = APIRouter()

@router.get("/api/projects")
async def list_projects():
    # Return all projects ordered by name, Default first

@router.post("/api/projects")
async def create_project(project: ProjectCreate):
    # Generate slug from name (slugify)
    # Insert into DB
    # Broadcast via WebSocket: { type: "project_created", project: {...} }

@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: int):
    # Prevent deleting Default project (id=1)
    # Reassign tasks to Default project OR reject if tasks exist
    # Broadcast: { type: "project_deleted", project_id: ... }
```

**Step 2: Make task endpoints project-aware**

Modify `app/routes/tasks.py`:
- `GET /api/tasks` — add optional `project_id` query param for server-side filtering
- `POST /api/tasks` — accept `project_id` in body (default: 1)
- `GET /api/tasks/{id}` — include `project_id` and project info in response
- `GET /api/config` — include projects list in config response

---

### Task 11: Add "Todo" to valid statuses

**Files:**
- Modify: `app/config.py` — already has "Todo" in VALID_STATUSES from Task 3

**Step 1: Verify status validation**

The `VALID_STATUSES` list already includes "Todo" from Task 3. Verify that:
- Creating a task with status "Todo" works
- Creating a task with an invalid status like "Foo" returns 422

### **CHECKPOINT 2** — Pause for user review and commit. Multi-project backend complete.

---

## Phase 3: Frontend — Todo Column, Project Switcher, Filters

### Task 12: Add "Todo" column to the board

**Files:**
- Modify: `static/index.html`

**Step 1: Update the board column definitions**

Find the JavaScript that defines the column order/statuses. Add "Todo" between "Backlog" and "In Progress". The column rendering loop should now produce 6 columns.

**Step 2: Update CSS for 6 columns**

Adjust the CSS grid/flex for the board to accommodate 6 columns instead of 5. Likely change:
```css
.board {
    grid-template-columns: repeat(6, 1fr);
    /* or adjust flex-basis percentages */
}
```

**Step 3: Ensure drag-and-drop works with new column**

Verify drag-and-drop handlers accept "Todo" as a valid drop target. The status is sent to `PATCH /api/tasks/{id}` which is already validated server-side.

---

### Task 13: Add Project Switcher to header

**Files:**
- Modify: `static/index.html`

**Step 1: Add project dropdown to header HTML**

Add a `<select>` or custom dropdown next to the board title:
```html
<div class="project-switcher">
    <select id="projectFilter" onchange="filterByProject(this.value)">
        <option value="all">All Projects</option>
        <!-- Populated dynamically -->
    </select>
</div>
```

**Step 2: Add JS functions**

```javascript
let currentProjectId = 'all';
let projects = [];

async function loadProjects() {
    const resp = await fetch('/api/projects');
    projects = await resp.json();
    populateProjectSwitcher();
}

function populateProjectSwitcher() {
    const select = document.getElementById('projectFilter');
    select.innerHTML = '<option value="all">All Projects</option>';
    projects.forEach(p => {
        select.innerHTML += `<option value="${p.id}">${escapeHtml(p.name)}</option>`;
    });
}

function filterByProject(projectId) {
    currentProjectId = projectId;
    renderBoard();
}
```

**Step 3: Update `renderBoard()` to filter by project**

In the existing `renderBoard()` function, add project filtering:
```javascript
let filteredTasks = tasks;
if (currentProjectId !== 'all') {
    filteredTasks = filteredTasks.filter(t => t.project_id == currentProjectId);
}
```

**Step 4: Add project badge to cards**

In `renderCard(task)`, add a badge when viewing "All Projects":
```javascript
if (currentProjectId === 'all') {
    const project = projects.find(p => p.id === task.project_id);
    if (project) {
        card.innerHTML += `<span class="project-badge" style="background:${project.color}">${escapeHtml(project.name)}</span>`;
    }
}
```

**Step 5: Add CSS for project badge**
```css
.project-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.7rem;
    color: white;
    opacity: 0.85;
}
```

---

### Task 14: Add Project Manager modal

**Files:**
- Modify: `static/index.html`

**Step 1: Add "Manage Projects" link near the project switcher**

Small link/button that opens a modal.

**Step 2: Create project manager modal**

Simple modal with:
- List of existing projects (name, color, delete button — disabled for Default)
- "Add Project" form: name input, color picker, description textarea, submit button
- Uses `POST /api/projects` and `DELETE /api/projects/{id}`
- Refreshes project list on changes

---

### Task 15: Add filter bar

**Files:**
- Modify: `static/index.html`

**Step 1: Add filter bar HTML below header**

```html
<div class="filter-bar" id="filterBar">
    <select id="filterPriority" onchange="applyFilters()">
        <option value="">All Priorities</option>
        <option value="Critical">Critical</option>
        <option value="High">High</option>
        <option value="Medium">Medium</option>
        <option value="Low">Low</option>
    </select>
    <select id="filterAgent" onchange="applyFilters()">
        <option value="">All Agents</option>
        <!-- Populated from config -->
    </select>
    <input type="text" id="filterSearch" placeholder="Search title/description..." oninput="applyFilters()">
</div>
```

**Step 2: Implement `applyFilters()` function**

```javascript
function applyFilters() {
    renderBoard(); // renderBoard reads filter values
}
```

**Step 3: Update `renderBoard()` to apply all filters**

```javascript
function getFilteredTasks() {
    let filtered = [...tasks];

    // Project filter
    if (currentProjectId !== 'all') {
        filtered = filtered.filter(t => t.project_id == currentProjectId);
    }

    // Priority filter
    const priority = document.getElementById('filterPriority').value;
    if (priority) filtered = filtered.filter(t => t.priority === priority);

    // Agent filter
    const agent = document.getElementById('filterAgent').value;
    if (agent) filtered = filtered.filter(t => t.agent === agent);

    // Search filter
    const search = document.getElementById('filterSearch').value.toLowerCase();
    if (search) {
        filtered = filtered.filter(t =>
            t.title.toLowerCase().includes(search) ||
            (t.description && t.description.toLowerCase().includes(search))
        );
    }

    return filtered;
}
```

**Step 4: Add CSS for filter bar**

```css
.filter-bar {
    display: flex;
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    background: var(--bg-dark);
    border-bottom: 1px solid rgba(255,255,255,0.05);
    flex-wrap: wrap;
}
.filter-bar select, .filter-bar input {
    background: var(--bg-card);
    color: var(--text);
    border: 1px solid rgba(255,255,255,0.1);
    padding: 0.4rem 0.6rem;
    border-radius: 6px;
    font-size: 0.85rem;
}
```

### **CHECKPOINT 3** — Pause for user review and commit. Todo column, project switcher, and filter bar functional.

---

## Phase 4: Responsive Design

### Task 16: Add responsive CSS media queries

**Files:**
- Modify: `static/index.html`

**Step 1: Add tablet breakpoint (768px)**

```css
@media (max-width: 768px) {
    .board {
        grid-template-columns: repeat(3, 1fr); /* 3 columns per row */
    }
    .filter-bar {
        flex-direction: column;
    }
    .task-modal {
        width: 95vw;
        max-width: none;
    }
    .command-bar-expanded {
        width: 95vw;
    }
}
```

**Step 2: Add mobile breakpoint (480px)**

```css
@media (max-width: 480px) {
    .board {
        grid-template-columns: 1fr; /* Single column stacking */
        gap: 0.5rem;
    }
    .header {
        flex-wrap: wrap;
    }
    .project-switcher {
        width: 100%;
    }
    .task-modal {
        width: 100vw;
        height: 100vh;
        border-radius: 0;
    }
    .task-modal .split-pane {
        flex-direction: column; /* Stack details + chat vertically */
    }
    .command-bar-expanded {
        width: 100vw;
        height: 100vh;
    }
}
```

**Step 3: Test at various viewport sizes**

Use browser dev tools to verify at 1920px, 768px, and 480px widths.

### **CHECKPOINT 4** — Pause for user review and commit. Responsive design complete.

---

## Phase 5: Markdown Export

### Task 17: Implement markdown export in task modal

**Files:**
- Modify: `static/index.html`

**Step 1: Add "Export MD" button to task modal header**

Add button next to existing header buttons (delete, save, close).

**Step 2: Implement `exportTaskMarkdown(taskId)` function**

```javascript
async function exportTaskMarkdown(taskId) {
    const task = tasks.find(t => t.id === taskId);
    if (!task) return;

    const taskComments = comments[taskId] || [];
    const taskActionItems = actionItems[taskId] || [];
    const project = projects.find(p => p.id === task.project_id);

    let md = `# ${task.title}\n\n`;
    md += `| Field | Value |\n|-------|-------|\n`;
    md += `| Status | ${task.status} |\n`;
    md += `| Priority | ${task.priority} |\n`;
    md += `| Agent | ${task.agent} |\n`;
    md += `| Project | ${project ? project.name : 'Default'} |\n`;
    md += `| Due Date | ${task.due_date || 'None'} |\n`;
    md += `| Created | ${task.created_at} |\n`;
    md += `| Updated | ${task.updated_at} |\n\n`;

    if (task.description) {
        md += `## Description\n\n${task.description}\n\n`;
    }

    if (taskActionItems.length > 0) {
        md += `## Action Items\n\n`;
        taskActionItems.forEach(item => {
            const check = item.resolved ? 'x' : ' ';
            md += `- [${check}] **${item.item_type}** (${item.agent}): ${item.content}\n`;
        });
        md += '\n';
    }

    if (taskComments.length > 0) {
        md += `## Comments\n\n`;
        taskComments.forEach(c => {
            md += `### ${c.agent} — ${formatDateTime(c.created_at)}\n\n`;
            md += `${c.content}\n\n---\n\n`;
        });
    }

    // Trigger download
    const blob = new Blob([md], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const slug = task.title.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 50);
    a.href = url;
    a.download = `task-${task.id}-${slug}.md`;
    a.click();
    URL.revokeObjectURL(url);
}
```

### **CHECKPOINT 5** — Pause for user review and commit. Markdown export complete.

---

## Phase 6: Agent Hardening

### Task 18: Guard against double-spawn

**Files:**
- Modify: `app/openclaw.py`

**Step 1: Add session liveness check**

```python
async def is_session_alive(session_key: str) -> bool:
    """Check if an OpenClaw session is still active."""
    try:
        resp = await httpx.AsyncClient().post(
            f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
            json={"tool": "sessions_list", "args": {"limit": 50}},
            headers={"Authorization": f"Bearer {OPENCLAW_TOKEN}"},
            timeout=5.0
        )
        sessions = resp.json().get("result", {}).get("sessions", [])
        return any(s.get("key") == session_key for s in sessions)
    except Exception:
        return False
```

**Step 2: Update `spawn_agent_session()` to check first**

Before spawning, check:
```python
async def spawn_agent_session(task_id, ...):
    db = get_db()
    task = db.execute("SELECT agent_session_key FROM tasks WHERE id = ?", (task_id,)).fetchone()

    if task and task["agent_session_key"]:
        if await is_session_alive(task["agent_session_key"]):
            logger.info(f"Task {task_id} already has active session, skipping spawn")
            return  # Don't double-spawn
        else:
            # Clear dead session
            db.execute("UPDATE tasks SET agent_session_key = NULL WHERE id = ?", (task_id,))
            db.commit()

    # Proceed with spawn...
```

---

### Task 19: Session liveness check on card open

**Files:**
- Modify: `app/routes/tasks.py` — add new endpoint
- Modify: `static/index.html` — call on modal open

**Step 1: Add backend endpoint**

```python
@router.get("/api/tasks/{task_id}/agent-status")
async def get_agent_status(task_id: int):
    db = get_db()
    task = db.execute("SELECT agent_session_key, working_agent FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(404)

    session_key = task["agent_session_key"]
    alive = False
    if session_key:
        alive = await is_session_alive(session_key)
        if not alive:
            # Clear stale data
            db.execute("UPDATE tasks SET agent_session_key = NULL, working_agent = NULL WHERE id = ?", (task_id,))
            db.commit()

    return {"alive": alive, "session_key": session_key, "working_agent": task["working_agent"]}
```

**Step 2: Call from frontend on modal open**

In the function that opens the task modal, add:
```javascript
// Check agent liveness
fetch(`/api/tasks/${taskId}/agent-status`)
    .then(r => r.json())
    .then(status => {
        if (!status.alive && workingAgentsByTask[taskId]) {
            // Clear working indicator
            delete workingAgentsByTask[taskId];
            renderBoard();
        }
        updateAgentControls(taskId, status);
    });
```

---

### Task 20: Auto-stop agent when task moves to Done

**Files:**
- Modify: `app/routes/tasks.py` — update move/update logic

**Step 1: In the task move/update handler, add auto-stop logic**

When status changes to "Done":
```python
if new_status == "Done":
    session_key = task["agent_session_key"]
    if session_key:
        try:
            # Stop the agent session
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
                    json={"tool": "sessions_stop", "args": {"sessionKey": session_key}},
                    headers={"Authorization": f"Bearer {OPENCLAW_TOKEN}"},
                    timeout=10.0
                )
            # Clear session data
            cursor.execute("UPDATE tasks SET agent_session_key = NULL, working_agent = NULL WHERE id = ?", (task_id,))
            logger.info(f"Auto-stopped agent session {session_key} for task {task_id} moved to Done")
        except Exception as e:
            logger.error(f"Failed to stop agent session: {e}")
```

### **CHECKPOINT 6** — Pause for user review and commit. Agent hardening complete.

---

## Phase 7: Docker & Cleanup

### Task 21: Update Docker configuration

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`

**Step 1: Update Dockerfile**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY static/ static/
VOLUME /app/data
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info", "--no-access-log"]
```

**Step 2: Test Docker build and run**

```bash
docker compose build && docker compose up -d
```

Verify app works in Docker container.

---

### Task 22: Final integration verification

**Step 1: Verify all features end-to-end**
- Create a project, switch to it, create tasks
- Drag tasks through all 6 columns
- Use filters (priority, agent, search)
- Export a task to markdown
- Verify agent spawns on In Progress, stops on Done
- Open card with agent session, verify liveness check
- Test responsive layouts at 768px and 480px
- Verify WebSocket real-time updates across all features

**Step 2: Keep `app.py` as backup**

Rename to `app.py.legacy` or keep `app.py.bak` that already exists.

### **CHECKPOINT 7** — Final review. Full refactor and feature expansion complete.
