# Changelog

All notable changes to this project will be documented in this file.

## [2.0.0] - 2026-02-14

### Architecture — Backend Refactor

- **Monolith → Package**: Refactored `app.py` (2224 lines) + `app.py.bak` (1976 lines) into modular `app/` package (15 modules, ~2800 lines)
- **Write-safe database layer**: Global write lock with `BEGIN IMMEDIATE`, WAL journal mode, 30s busy timeout for concurrent reads
- **Request logging middleware**: Batched 0.5s windows, groups by method:pattern, shows count/avg duration/errors
- **IP restriction middleware**: Configurable allowed IPs (localhost + Docker networks + env-based)
- **Dockerfile**: Updated to `uvicorn app.main:app`, copies `app/` directory

### Added — Multi-Project Support

- **Projects CRUD**: `POST /api/projects`, `DELETE /api/projects/{id}` with slug generation and duplicate check
- **Project switcher**: Dropdown in header to filter tasks by project; "All Projects" shows colored badges on cards
- **Project manager modal**: Add/remove projects with name, color picker, description
- **Task assignment**: `project_id` field on tasks (default: 1 = "Default"), project dropdown in task form

### Added — Filtering & UI

- **Filter bar**: Combined priority, agent, and keyword search filters (client-side AND logic)
- **"Todo" status column**: New column between Backlog and In Progress (6 columns total: Backlog → Todo → In Progress → Review → Done → Blocked)
- **Markdown export**: "Export MD" button in task modal — exports title, metadata table, description, action items (as checklist), and comments as downloadable `.md`
- **Auto-save task fields**: Status, priority, agent, project, due date, and description auto-save on change/blur — no more manual save for field updates
- **Inline action item creation**: "+ Add" button in action items section with type picker (question/blocker/completion) and inline text input
- **Improved task modal layout**: Larger modal (720×700px), 8-row description textarea, project field moved to first row, form fields split into two rows
- **Better save feedback**: Longer glow animation (2.5s), green background pulse, white icon with glow filter
- **Resolved action items**: Green checkmark (#10b981) instead of muted gray
- **Project switcher moved to filter bar**: Cleaner header with "Projects" button for project manager

### Added — Agent Management

- **Dynamic agent detection**: Three-tier priority: `AGENTS` env var → OpenClaw API auto-detect → hardcoded fallback
- **Agent metadata from API**: Icons, colors, descriptions fetched from OpenClaw at startup; dynamic CSS injection for agent tags
- **`AUTO_STOP_ON_DONE`**: Configurable via `.env` (default: `true`). When enabled, sessions are permanently deleted on Done or Stop. Set `false` to use OpenClaw's `archiveAfterMinutes` instead.
- **Session liveness check**: `GET /api/tasks/{id}/agent-status` — checks if OpenClaw session is alive
- **Toggle start/stop button**: Replaces old stop-only button; shows ▶ (green) / ■ (red) based on state
- **Double-spawn guard**: `_spawning_tasks` set prevents concurrent spawns for same task
- **Follow-up spawning**: `spawn_followup_session()` with last 5 comments as context
- **@Mention spawning**: `spawn_mentioned_agent()` with cleanup=delete

### Added — Session Management

- **WebSocket RPC**: `_ws_rpc()` for OpenClaw gateway communication (challenge → connect → request → response)
- **Session endpoints**: `POST /api/sessions/create`, `POST /api/sessions/{key}/stop`, `POST /api/sessions/{key}/delete`, `POST /api/sessions/stop-all`
- **Session list**: Sorted main-first, then by updatedAt

### Added — Validation & Security

- **Pydantic field validators**: Status/priority validated against `VALID_STATUSES`/`VALID_PRIORITIES`, returns HTTP 422 on invalid values
- **Model validation**: Comment content size limit, agent name length limit, color pattern validation on projects
- **Agent guardrails**: Filesystem boundaries, forbidden actions, compliance context, escalation chain, report format template

### Added — Database Schema

- `tasks.working_agent` (TEXT) — currently active agent
- `tasks.agent_session_key` (TEXT) — OpenClaw session key
- `tasks.source_file` (TEXT) — source file reference
- `tasks.source_ref` (TEXT) — source ref
- `tasks.project_id` (INTEGER, FK → projects) — project assignment
- `action_items.archived` (INTEGER) — archive support
- `chat_messages.session_key` (TEXT) — session isolation for chat history
- `projects` table — multi-project support

### Added — Environment Variables

- `AGENT_AUTO_DETECT` — Auto-detect agents from OpenClaw API (default: `true`)
- `AGENTS` — Manual agent list, format: `agent_id:Name,...` (overrides auto-detect)
- `AUTO_STOP_ON_DONE` — Auto-kill sessions on Done (default: `true`)
- `TASKBOARD_BASE_URL` — Public URL for CORS/agent prompts (default: `http://localhost:8080`)
- `ALLOWED_IPS` — Additional allowed IPs (comma-separated)
- `PROJECT_NAME`, `COMPANY_NAME`, `COMPANY_CONTEXT` — Agent prompt context
- `ALLOWED_PATHS` — Filesystem boundaries for agents
- `COMPLIANCE_FRAMEWORKS` — Compliance context for security auditor

### Changed

- **Board layout**: Column flex from `1 0 300px` to `1 0 250px`, board height accounts for filter bar
- **Agent colors**: Removed hardcoded CSS classes, now dynamically injected from `config.agentMeta`
- **Agent legend**: Dynamically populated from API metadata instead of hardcoded list
- **Task loading**: Removed server-side agent filter (`?agent=`), all filtering now client-side
- **Help modal**: Agent list dynamically generated from `config.agentMeta`
- **Action items**: Added archive/unarchive support (`POST /api/action-items/{id}/archive|unarchive`)
- **Chat history**: Filtered by `session_key` parameter
- **Comment posting**: Auto-clears `working_agent` when agent posts; builds 5-comment context for follow-ups

### Fixed

- **Status enforcement**: Backend rejects invalid statuses/priorities with HTTP 422 instead of silently accepting

## [1.3.0] - 2026-02-03

### Added
- **Chat message actions**: Reply, copy, and delete buttons on all chat messages
  - Reply (↩) — Shows preview above input, supports multi-reply (reply to multiple messages at once)
  - Copy (📋) — Copies message content with fallback for non-HTTPS contexts
  - Delete (🗑) — Removes message with confirmation (clears context or secrets)
- **DELETE endpoint for comments**: `DELETE /api/tasks/{id}/comments/{comment_id}` with WebSocket broadcast
- **Multi-reply support**: Click reply on multiple messages, each shows as stacked preview with "Clear all" button

### Changed
- **Command bar chat size**: Increased from 600×400px to 720×500px for better readability
- **Event delegation**: All chat button handlers now use event delegation (fixes special character issues in message content)

### Fixed
- **Reply button on assistant messages**: Fixed selector mismatch (`.command-chat-input-area` vs `.jarvis-chat-input-area`)
- **@Mention spawn logic**: Only explicitly @mentioned agents are spawned now — assigned agent no longer auto-spawns when other agents are tagged
- **Inline onclick handlers**: Replaced with data attributes + event delegation to handle messages with quotes, newlines, and special characters

## [1.2.0] - 2026-02-02

### Added
- **Image attachments for command bar chat**: Images now saved to `/data/attachments/` and passed as readable file paths to agents
- **Sub-agent guardrails documentation**: Updated `examples/dev-team-example.md` with comprehensive guardrails including:
  - Identity rules (main agent clones vs domain-specific agents)
  - Filesystem boundaries
  - Git safeword requirements
  - Browser access matrix (UX Manager only)
  - Compliance context templates

### Changed
- **Default column sort**: Changed from "Priority" to "Latest" (most recent first)
- **Theater mode spacing**: Tightened padding throughout for more conversation space
  - Chat header: 0.75rem → 0.5rem
  - Chat messages margin: 0.75rem → 0.25rem
  - Chat input area: 0.75rem/1rem → 0.25rem

### Fixed
- **Double image paste bug**: Removed duplicate `onpaste` handler that was causing images to paste twice
- **Card bottom border radius**: Added `border-radius: 0 0 16px 16px` to chat-input-area so modal corners are visible
- **UX Manager browser privilege**: Clarified in dev-team template that UX Manager is the ONLY agent with browser access; others must request their help

## [1.1.0] - 2026-01-31

### Added
- **Identity emoji support**: Command bar icon now uses `MAIN_AGENT_EMOJI` from environment variable (defaults to 🛡️)
- **Multi-line input**: Chat input is now a textarea supporting Shift+Enter for new lines
- **Graceful WebSocket reconnect**: Shows glowing indicator during reconnection instead of error messages
- **Retry logic**: API calls retry up to 3 times with exponential backoff for transient failures

### Changed
- **Scrollbar styling**: Thin, styled scrollbar (6px) that doesn't overlap borders
- **Textarea auto-resize**: Input grows with content up to 150px max height
- **Textarea reset**: Input resets to single line after sending message
- **Thinking indicator**: Moved from input area to header as glowing shield icon
- **Placeholder text**: Shortened to "Ctrl+V to paste images · Shift+Enter for new line"

### Fixed
- **Button alignment**: Attach, input, and send buttons now properly aligned at 44px height
- **OCD-compliant symmetry**: All input row elements use consistent sizing and box-sizing

## [1.0.0] - 2026-01-28

### Added
- Initial release
- Kanban board with Backlog, In Progress, Review, Done, Blocked columns
- Agent assignment and management
- Real-time WebSocket updates
- Agent chat integration via OpenClaw
- Task comments and action items
- Priority levels (Critical, High, Medium, Low)
- Agent work indicators
