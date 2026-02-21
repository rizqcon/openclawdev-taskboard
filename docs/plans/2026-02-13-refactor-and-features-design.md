# OpenDevBoard Refactor & Feature Expansion — Design Document

**Date:** 2026-02-13
**Status:** Approved

---

## 1. Backend Architecture

### Package Structure

```
app/
├── __init__.py          # create_app() factory
├── main.py              # Uvicorn entry, middleware registration
├── config.py            # Env-based settings
├── database.py          # get_db(), init_db(), schema migrations
├── models.py            # Pydantic request/response schemas
├── websocket.py         # ConnectionManager, broadcast helpers
├── openclaw.py          # Agent spawn/send/stop helpers
└── routes/
    ├── __init__.py      # Collects all APIRouters
    ├── tasks.py         # /api/tasks CRUD, /move, /start-work, /stop-work
    ├── projects.py      # /api/projects CRUD
    ├── comments.py      # /api/tasks/{id}/comments
    ├── action_items.py  # /api/tasks/{id}/action-items + resolve/archive
    ├── sessions.py      # /api/sessions CRUD, stop, stop-all
    ├── chat.py          # /api/jarvis/chat, /history, /respond
    └── uploads.py       # /api/upload/image
```

### Database Changes

New `projects` table:

```sql
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    color TEXT DEFAULT '#00b4d8',
    created_at TEXT NOT NULL
);
```

Default project auto-inserted on first migration.

`tasks` table addition:

```sql
ALTER TABLE tasks ADD COLUMN project_id INTEGER DEFAULT 1 REFERENCES projects(id);
```

All existing tasks assigned to Default project (id=1).

### Status Validation

Valid statuses: `["Backlog", "Todo", "In Progress", "Review", "Done", "Blocked"]`

Backend rejects any task create/update with invalid status (HTTP 422).

### Docker Updates

- Dockerfile: `COPY app/ app/` replaces `COPY app.py .`
- CMD: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --log-level info --no-access-log`

---

## 2. Frontend Changes

### Project Switcher

- Dropdown in header, next to board title
- Options: "All Projects" (default), then each project by name
- Selecting filters tasks client-side
- "Manage Projects" link opens add/remove modal

### Project Badges

- In "All Projects" view: colored pill badge on each card with project name
- Badge color from `project.color`
- Hidden when viewing single project

### New "Todo" Column

- Column order: Backlog → Todo → In Progress → Review → Done → Blocked (6 columns)
- Full drag-and-drop support

### Responsive Design

- Breakpoints: 768px (tablet), 480px (mobile)
- Tablet: columns wrap 2-3 per row
- Mobile: columns stack vertically, full-width. Modals go full-screen
- Command bar full-width on mobile

### Filter Bar

- Below header, above board
- Dropdowns: Priority, Agent, Project (in "All" view)
- Text search: title/description keyword
- AND logic, client-side filtering

### Markdown Export

- "Export MD" button in task modal header
- Content: title, metadata table (priority, status, agent, due date, project), description, action items, all comments chronologically
- Downloads as `task-{id}-{title-slug}.md`

---

## 3. Agent Hardening

### Guard Against Double-Spawn

- Before spawning, check if `task.agent_session_key` is set AND session is active via OpenClaw `sessions_list`
- Active session → skip spawn
- Dead session → clear `agent_session_key`, spawn new

### Session Liveness Check on Card Open

- When opening task modal, if `agent_session_key` exists, background-check session liveness
- Update working indicator (clear if dead)
- New endpoint: `GET /api/tasks/{task_id}/agent-status` → `{ alive: bool, session_key: str }`

### Agent Lifecycle on Status Change

- Moving to "In Progress" → auto-spawn agent (if assigned and no active session)
- Moving to "Done" → auto-stop agent session (if one exists)
- All other transitions → agent stays alive

---

## 4. Constraints

- Tech stack: FastAPI, SQLite, Vanilla JS/CSS (no frameworks)
- Preserve: WebSocket live updates, AI-Agent integration, security middleware (IP restriction, API keys)
- Maintain dark/cyberpunk aesthetic
- After every major task, pause for user review and commit
