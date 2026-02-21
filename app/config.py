"""
Configuration: environment variables, constants, agent metadata.
"""

import os
import re
from pathlib import Path
from typing import List, Optional

# =============================================================================
# PATHS
# =============================================================================
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "tasks.db"
STATIC_PATH = Path(__file__).parent.parent / "static"
ATTACHMENTS_PATH = DATA_DIR / "attachments"
ATTACHMENTS_PATH.mkdir(exist_ok=True)

# =============================================================================
# BRANDING
# =============================================================================
MAIN_AGENT_NAME = os.getenv("MAIN_AGENT_NAME", "Assistant")
MAIN_AGENT_EMOJI = os.getenv("MAIN_AGENT_EMOJI", "\U0001F6E1")
HUMAN_NAME = os.getenv("HUMAN_NAME", "User")
HUMAN_SUPERVISOR_LABEL = os.getenv("HUMAN_SUPERVISOR_LABEL", "User")
BOARD_TITLE = os.getenv("BOARD_TITLE", "Task Board")

# =============================================================================
# VALID STATUSES & PRIORITIES (used for validation)
# =============================================================================
VALID_STATUSES = ["Backlog", "Todo", "In Progress", "Review", "Done", "Blocked"]
VALID_PRIORITIES = ["Critical", "High", "Medium", "Low"]

# Legacy list used by /api/config (kept as-is from original)
STATUSES = ["Backlog", "Todo", "In Progress", "Review", "Done", "Blocked"]
PRIORITIES = ["Critical", "High", "Medium", "Low"]

# =============================================================================
# AGENT COLOR POOL (for agents without a color from OpenClaw)
# =============================================================================
_AGENT_COLORS = ["#6366f1", "#8b5cf6", "#ef4444", "#14b8a6", "#ec4899", "#f59e0b", "#06b6d4", "#84cc16", "#a855f7", "#f43f5e", "#64748b"]

# Mutable agent state (populated at startup)
AGENTS: List[str] = []
AGENT_TO_OPENCLAW_ID: dict = {}
AGENT_META: dict = {}
MENTIONABLE_AGENTS: List[str] = []
MENTION_PATTERN = None


def _rebuild_mention_pattern():
    global MENTIONABLE_AGENTS, MENTION_PATTERN
    MENTIONABLE_AGENTS = list(AGENT_TO_OPENCLAW_ID.keys())
    if MENTIONABLE_AGENTS:
        MENTION_PATTERN = re.compile(r'@(' + '|'.join(re.escape(a) for a in MENTIONABLE_AGENTS) + r')', re.IGNORECASE)
    else:
        MENTION_PATTERN = re.compile(r'(?!)')  # never matches


def _build_agents_from_env():
    """Build agent lists from AGENTS env var. Format: 'agent_id:Display Name,agent_id2:Name2'"""
    global AGENTS, AGENT_TO_OPENCLAW_ID, AGENT_META, MENTIONABLE_AGENTS, MENTION_PATTERN
    AGENT_TO_OPENCLAW_ID = {}
    AGENT_META = {}
    color_idx = 0

    for entry in AGENTS_ENV.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            agent_id, agent_name = entry.split(":", 1)
            agent_id = agent_id.strip()
            agent_name = agent_name.strip()
        else:
            agent_id = entry.strip()
            agent_name = agent_id.replace("-", " ").title()

        AGENT_TO_OPENCLAW_ID[agent_name] = agent_id
        icon = MAIN_AGENT_EMOJI if agent_id == "main" else "🤖"
        color = _AGENT_COLORS[color_idx % len(_AGENT_COLORS)]
        color_idx += 1
        AGENT_META[agent_name] = {"id": agent_id, "icon": icon, "color": color, "description": ""}

    AGENTS = list(AGENT_TO_OPENCLAW_ID.keys()) + ["User", "Unassigned"]
    AGENT_META["User"] = {"id": "user", "icon": "👤", "color": "#22c55e", "description": "Human supervisor"}
    AGENT_META["Unassigned"] = {"id": "unassigned", "icon": "○", "color": "#64748b", "description": "Not yet assigned"}
    _rebuild_mention_pattern()
    print(f"⚙️ AGENTS (from ENV): {AGENTS}")


def _build_fallback_agents():
    """Minimal fallback when OpenClaw is unreachable — only main agent + User + Unassigned."""
    global AGENTS, AGENT_TO_OPENCLAW_ID, AGENT_META, MENTIONABLE_AGENTS, MENTION_PATTERN
    AGENT_TO_OPENCLAW_ID = {MAIN_AGENT_NAME: "main"}
    AGENT_META = {
        MAIN_AGENT_NAME: {"id": "main", "icon": MAIN_AGENT_EMOJI, "color": "#6366f1", "description": "Main coordinator"},
        "User": {"id": "user", "icon": "\U0001f464", "color": "#22c55e", "description": "Human supervisor"},
        "Unassigned": {"id": "unassigned", "icon": "\u25cb", "color": "#64748b", "description": "Not yet assigned"},
    }
    AGENTS = [MAIN_AGENT_NAME, "User", "Unassigned"]
    _rebuild_mention_pattern()
    print(f"\u2699\ufe0f AGENTS (fallback — connect OpenClaw for full agent list): {AGENTS}")


def _populate_agents_from_openclaw(agents_data: list):
    """Build agent lists from OpenClaw API response."""
    global AGENTS, AGENT_TO_OPENCLAW_ID, AGENT_META, MENTIONABLE_AGENTS, MENTION_PATTERN
    AGENT_TO_OPENCLAW_ID = {}
    AGENT_META = {}
    color_idx = 0

    for agent in agents_data:
        agent_id = agent["id"]
        agent_name = agent["name"]

        if agent_id == "main":
            agent_name = MAIN_AGENT_NAME
            icon = MAIN_AGENT_EMOJI
        else:
            icon = agent.get("icon", "🤖")

        color = agent.get("color") or _AGENT_COLORS[color_idx % len(_AGENT_COLORS)]
        color_idx += 1
        description = agent.get("description", f"{agent_name} agent")

        AGENT_TO_OPENCLAW_ID[agent_name] = agent_id
        AGENT_META[agent_name] = {"id": agent_id, "icon": icon, "color": color, "description": description}

    AGENTS = list(AGENT_TO_OPENCLAW_ID.keys()) + ["User", "Unassigned"]
    AGENT_META["User"] = {"id": "user", "icon": "\U0001f464", "color": "#22c55e", "description": "Human supervisor"}
    AGENT_META["Unassigned"] = {"id": "unassigned", "icon": "\u25cb", "color": "#64748b", "description": "Not yet assigned"}
    _rebuild_mention_pattern()
    print(f"\u2705 AGENTS (from OpenClaw): {AGENTS}")


# Initialize with fallback; replaced at startup if OpenClaw is reachable
_build_fallback_agents()

# =============================================================================
# SECURITY / TOKENS
# =============================================================================
import secrets

OPENCLAW_GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://host.docker.internal:18789")
OPENCLAW_TOKEN = os.getenv("OPENCLAW_TOKEN", "")
TASKBOARD_API_KEY = os.getenv("TASKBOARD_API_KEY", "")
TASKBOARD_BASE_URL = os.getenv("TASKBOARD_BASE_URL", "http://localhost:8080")
OPENCLAW_ENABLED = bool(OPENCLAW_TOKEN)
AGENT_AUTO_DETECT = os.getenv("AGENT_AUTO_DETECT", "true").lower() in ("true", "1", "yes")
# Auto-stop agent sessions when a task is moved to Done
AUTO_STOP_ON_DONE = os.getenv("AUTO_STOP_ON_DONE", "true").lower() in ("true", "1", "yes")
# Comma-separated list of agent_id:Display Name pairs, e.g. "main:Assistant,architect:Architect"
AGENTS_ENV = os.getenv("AGENTS", "")

# Project configuration
PROJECT_NAME = os.getenv("PROJECT_NAME", "My Project")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Acme Corp")
COMPANY_CONTEXT = os.getenv("COMPANY_CONTEXT", "software development")
ALLOWED_PATHS = os.getenv("ALLOWED_PATHS", "/workspace, /project")
COMPLIANCE_FRAMEWORKS = os.getenv("COMPLIANCE_FRAMEWORKS", "your security requirements")

# IP-based access restriction
ALWAYS_ALLOWED_IPS = {"127.0.0.1", "localhost", "::1"}
_env_ips = os.getenv("ALLOWED_IPS", "").strip()
ALLOWED_IPS = set(ip.strip() for ip in _env_ips.split(",") if ip.strip()) if _env_ips else set()
print(f"\U0001f512 IP Restriction: localhost + 172.20.200.59 + 172.20.200.119 + 172.18.0.1 (internal) + {ALLOWED_IPS if ALLOWED_IPS else 'no external IPs'}")

if not TASKBOARD_API_KEY:
    print("\u26a0\ufe0f  WARNING: TASKBOARD_API_KEY not set. API authentication disabled!")
if not OPENCLAW_TOKEN:
    print("\u26a0\ufe0f  WARNING: OPENCLAW_TOKEN not set. OPENCLAW integration disabled!")

# File upload limits
MAX_ATTACHMENT_SIZE_MB = 10
MAX_ATTACHMENT_SIZE_BYTES = MAX_ATTACHMENT_SIZE_MB * 1024 * 1024

# Specific Docker IPs allowed
ALLOWED_DOCKER_IPS = {
    "172.20.200.59",
    "172.20.200.119",
    "172.18.0.1",
}

# CORS allowed origins
ALLOWED_ORIGINS = [
    "{TASKBOARD_BASE_URL}",
    "http://127.0.0.1:8080",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
if TASKBOARD_BASE_URL not in ALLOWED_ORIGINS:
    ALLOWED_ORIGINS.append(TASKBOARD_BASE_URL)
    if TASKBOARD_BASE_URL.startswith("https://"):
        http_variant = TASKBOARD_BASE_URL.replace("https://", "http://")
        if http_variant not in ALLOWED_ORIGINS:
            ALLOWED_ORIGINS.append(http_variant)
