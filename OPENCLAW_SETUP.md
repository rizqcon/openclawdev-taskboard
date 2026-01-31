# 🤖 OPENCLAW Integration Guide

This document explains how to configure OPENCLAW to work with the OPENCLAW Task Board's multi-agent system.

## Overview

When you drag a task to "In Progress", the task board automatically:
1. Calls OPENCLAW's `/tools/invoke` API
2. Spawns a sub-agent session with `sessions_spawn`
3. Passes the task details and guardrails to the agent
4. The agent posts updates as comments on the task

**For this to work, you need agents configured in OPENCLAW.**

---

## Quick Start (Minimal Setup)

If you just want the task board without multi-agent automation:

1. Set `OPENCLAW_ENABLED=false` in your `.env` (or leave `OPENCLAW_TOKEN` empty)
2. The board works as a standard Kanban — no AI sessions spawn
3. You can still use it for manual task tracking

---

## Full Multi-Agent Setup

### Step 1: Configure OPENCLAW Agents

Add agents to your OPENCLAW config (`config.yaml` or via `OPENCLAW config`):

```yaml
agents:
  architect:
    model: anthropic/claude-sonnet-4-20250514  # or your preferred model
    systemPrompt: |
      You are the Architect for a software project.
      Focus on system design, patterns, scalability, and technical trade-offs.
      Be concise. Flag concerns with severity (CRITICAL/HIGH/MEDIUM/LOW).
    
  security-auditor:
    model: anthropic/claude-sonnet-4-20250514
    systemPrompt: |
      You are a Security Auditor.
      Focus on SOC2, HIPAA, CIS Controls compliance.
      Review for vulnerabilities, credential handling, data isolation.
      Rate findings: CRITICAL (blocks deploy) / HIGH / MEDIUM / LOW
    
  code-reviewer:
    model: anthropic/claude-sonnet-4-20250514
    systemPrompt: |
      You are a Code Reviewer.
      Focus on Python/Django best practices, DRY, SOLID, error handling.
      Format: MUST FIX / SHOULD FIX / CONSIDER / NICE TO HAVE
    
  ux-manager:
    model: anthropic/claude-sonnet-4-20250514
    systemPrompt: |
      You are a UX Manager.
      Focus on user flows, error messages, form design, accessibility.
      You have browser access to localhost only for UI review.
```

### Step 2: Get Your OPENCLAW Token

Your gateway token is in your OPENCLAW config. Find it:

```bash
OPENCLAW config get gateway.token
```

Or check your `config.yaml`:
```yaml
gateway:
  token: "your-token-here"
```

### Step 3: Configure Task Board

Create `.env` from the example:

```bash
cp .env.example .env
```

Edit `.env`:
```env
OPENCLAW_GATEWAY_URL=http://host.docker.internal:18789
OPENCLAW_TOKEN=your-OPENCLAW-token-here
TASKBOARD_API_KEY=generate-a-random-key
```

**Note:** Use `host.docker.internal` if running the task board in Docker and OPENCLAW on your host machine.

### Step 4: Restart Task Board

```bash
docker-compose down
docker-compose up -d
```

---

## Agent ID Mapping

The task board maps display names to OPENCLAW agent IDs:

| Task Board Name | OPENCLAW Agent ID |
|-----------------|-------------------|
| OpenClaw | `main` |
| Architect | `architect` |
| Security Auditor | `security-auditor` |
| Code Reviewer | `code-reviewer` |
| UX Manager | `ux-manager` |

### Customizing Agent IDs

If your OPENCLAW uses different agent IDs, edit `app.py`:

```python
AGENT_TO_OPENCLAW_ID = {
    "OpenClaw": "main",
    "Architect": "your-architect-id",
    "Security Auditor": "your-security-id",
    # ... etc
}
```

---

## Command Bar Setup (Two-Way Chat)

The command bar lets you chat with OpenClaw directly. For responses to appear in the command bar:

### 1. Add to Your OPENCLAW's TOOLS.md

```markdown
## Command Bar (Task Board Two-Way Chat)

When a message arrives prefixed with `[COMMAND_BAR]`, respond via the task board endpoint:

\`\`\`powershell
Invoke-RestMethod -Uri "http://localhost:8080/api/<Moltname>/respond" -Method POST -Headers @{"Content-Type"="application/json"; "X-API-Key"="YOUR_TASKBOARD_API_KEY"} -Body '{"response":"Your message here"}'
\`\`\`

This pushes the response to the command bar via WebSocket for real-time two-way chat.
```

### 2. How It Works

1. You type in the command bar
2. Task board sends `[COMMAND_BAR] your message` to OPENCLAW via wake
3. Molt (main agent) receives the message
4. Molt responds by POSTing to `/api/<Moltname>/respond`
5. Task board broadcasts response via WebSocket
6. You see the response in the command bar

---

## Agent Guardrails

When agents are spawned, they receive these guardrails:

```
⚠️ MANDATORY CONSTRAINTS:

FILESYSTEM BOUNDARIES:
- ONLY access: [your configured paths]
- Everything else is FORBIDDEN

FORBIDDEN ACTIONS:
- Browser tool (except UX Manager on localhost)
- git commit (requires approval)
- External API calls (requires approval)

COMMUNICATION:
- Post comments on task cards
- Create action items for questions
- Move to Review when done
```

You can customize guardrails in `app.py` → `AGENT_GUARDRAILS`.

---

## Troubleshooting

### Sessions not spawning?

1. **Check OPENCLAW is running:**
   ```bash
   OPENCLAW status
   ```

2. **Check the token is correct:**
   ```bash
   curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:18789/api/status
   ```

3. **Check agent exists:**
   ```bash
   OPENCLAW config get agents.architect
   ```

4. **Check Docker can reach host:**
   - Use `host.docker.internal` on Docker Desktop
   - On Linux, you may need `--network host` or the host IP

### Command bar not responding?

1. Ensure TOOLS.md has the command bar instructions
2. Check `TASKBOARD_API_KEY` matches in both places
3. Look for `[COMMAND_BAR]` prefix in OPENCLAW logs

### Agents not posting comments?

The agent needs to make HTTP calls to the task board. Ensure:
1. Agent can reach `http://localhost:8080` (or your task board URL)
2. No firewall blocking the connection
3. Check agent logs for HTTP errors

---

## Architecture Flow

```
┌─────────────────────────────────────────────────────────────┐
│                      Task Board UI                          │
│   User drags task to "In Progress"                         │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                   Task Board Backend                        │
│   POST /tools/invoke { sessions_spawn, agentId, task }     │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                   OPENCLAW Gateway                          │
│   Spawns sub-agent session with task prompt + guardrails   │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                    Agent Session                            │
│   - Analyzes task                                          │
│   - POSTs comments to task board                           │
│   - Creates action items if needed                         │
│   - Moves task to Review when done                         │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                   Task Board Backend                        │
│   Receives comment → Broadcasts via WebSocket              │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                      Task Board UI                          │
│   Live update: comment appears on task card                │
└─────────────────────────────────────────────────────────────┘
```

---

## Need Help?

- [OPENCLAW Documentation](https://docs.Molt.bot)
- [OPENCLAW Discord](https://discord.com/invite/Molt)
- [GitHub Issues](https://github.com/rizqcon/moltdev-taskboard/issues)
