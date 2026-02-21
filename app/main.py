"""
RIZQ Task Board - FastAPI Backend
Main app: middleware, WebSocket endpoint, startup, static serving.
"""

import httpx
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import (
    STATIC_PATH, ALLOWED_ORIGINS, ALWAYS_ALLOWED_IPS, ALLOWED_DOCKER_IPS, ALLOWED_IPS,
    OPENCLAW_ENABLED, OPENCLAW_GATEWAY_URL, OPENCLAW_TOKEN,
    ATTACHMENTS_PATH, DATA_DIR,
    AGENT_AUTO_DETECT, AGENTS_ENV,
    _populate_agents_from_openclaw, _build_agents_from_env,
)
from app.openclaw import fetch_agent_identities
from app.database import init_db
from app.websocket import manager
from app.routes import api_router

# =============================================================================
# APP
# =============================================================================

app = FastAPI(title="RIZQ Task Board", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type"],
)


# IP Restriction Middleware
class IPRestrictionMiddleware(BaseHTTPMiddleware):
    """Block requests from IPs not in the allowed list."""

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else None
        if client_ip in ALWAYS_ALLOWED_IPS:
            return await call_next(request)
        if client_ip in ALLOWED_DOCKER_IPS:
            return await call_next(request)
        if client_ip in ALLOWED_IPS:
            return await call_next(request)
        print(f"\U0001f6ab Blocked request from {client_ip} - not in allowed IPs")
        return PlainTextResponse(f"Access denied. IP {client_ip} not authorized.", status_code=403)


app.add_middleware(IPRestrictionMiddleware)


# Request Logging Middleware
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Batch API requests and log summaries to reduce spam."""

    def __init__(self, app):
        super().__init__(app)
        self.batch = []
        self.batch_window = 0.5
        self.last_flush = datetime.now().timestamp()

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(("/static/", "/data/")):
            return await call_next(request)

        path = request.url.path
        method = request.method
        client_ip = request.client.host if request.client else "unknown"

        start_time = datetime.now()
        response = await call_next(request)
        duration = (datetime.now() - start_time).total_seconds()

        if path.startswith("/api/"):
            pattern = self._get_pattern(path)
            task_id = self._extract_task_id(path)
            self.batch.append({
                "method": method, "pattern": pattern, "path": path,
                "task_id": task_id, "status": response.status_code,
                "duration": duration, "client_ip": client_ip
            })
            now = datetime.now().timestamp()
            if now - self.last_flush >= self.batch_window and self.batch:
                self._flush_batch()
        elif method in ["POST", "PATCH", "DELETE", "PUT"]:
            emoji = self._get_emoji(method, response.status_code)
            status_emoji = "\u2705" if 200 <= response.status_code < 300 else "\u26a0\ufe0f" if response.status_code < 500 else "\u274c"
            print(f"{emoji} {method} {path} - {status_emoji} {response.status_code} ({duration:.3f}s)")

        return response

    def _flush_batch(self):
        if not self.batch:
            return
        groups = {}
        for req in self.batch:
            key = f"{req['method']}:{req['pattern']}"
            if key not in groups:
                groups[key] = {"requests": [], "task_ids": set(), "total_duration": 0, "errors": 0}
            groups[key]["requests"].append(req)
            if req["task_id"]:
                groups[key]["task_ids"].add(req["task_id"])
            groups[key]["total_duration"] += req["duration"]
            if req["status"] >= 400:
                groups[key]["errors"] += 1
        total = len(self.batch)
        print(f"\n\U0001f4ca {total} requests ({self.batch_window}s):")
        for key, group in groups.items():
            method, pattern = key.split(":", 1)
            count = len(group["requests"])
            task_ids = sorted(group["task_ids"])
            avg_duration = group["total_duration"] / count
            emoji = self._get_emoji(method, 200)
            task_info = f" [{','.join(map(str, task_ids[:10]))}{'...' if len(task_ids) > 10 else ''}]" if task_ids else ""
            error_info = f" \u26a0\ufe0f{group['errors']}" if group['errors'] > 0 else ""
            print(f"  {emoji} {method} {pattern}: {count}x @ {avg_duration:.3f}s{task_info}{error_info}")
        self.batch = []
        self.last_flush = datetime.now().timestamp()

    def _get_pattern(self, path: str) -> str:
        import re
        return re.sub(r'/\d+(/|$)', '/{id}\\1', path)

    def _extract_task_id(self, path: str) -> int:
        if "/tasks/" in path:
            parts = path.split("/tasks/")
            if len(parts) > 1:
                id_part = parts[1].split("/")[0]
                if id_part.isdigit():
                    return int(id_part)
        return None

    def _get_emoji(self, method: str, status: int) -> str:
        if status >= 400:
            return "\u274c"
        return {"GET": "\U0001f4d6", "POST": "\U0001f4dd", "PATCH": "\u270f\ufe0f", "PUT": "\U0001f4e4", "DELETE": "\U0001f5d1\ufe0f"}.get(method, "\U0001f537")


app.add_middleware(RequestLoggingMiddleware)

# Include all API routes
app.include_router(api_router)


# Startup
@app.on_event("startup")
async def startup():
    init_db()
    # Agent detection: ENV > Auto-detect from OpenClaw > Fallback defaults
    if AGENTS_ENV:
        _build_agents_from_env()
    elif AGENT_AUTO_DETECT and OPENCLAW_ENABLED:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
                    json={"tool": "agents_list", "args": {}},
                    headers={"Authorization": f"Bearer {OPENCLAW_TOKEN}", "Content-Type": "application/json"}
                )
                result = response.json() if response.status_code == 200 else None
                if result and result.get("ok"):
                    raw_result = result.get("result", {})
                    details = raw_result.get("details", raw_result)
                    agents_data = details.get("agents", [])
                    if agents_data:
                        _populate_agents_from_openclaw(agents_data)
                        await fetch_agent_identities(agents_data)
                    else:
                        print("\u26a0\ufe0f OpenClaw returned empty agents list, using fallback")
                else:
                    print(f"\u26a0\ufe0f OpenClaw agents_list failed: {response.status_code}, using fallback")
        except Exception as e:
            print(f"\u26a0\ufe0f Could not fetch agents from OpenClaw: {e}, using fallback")
    else:
        if not AGENT_AUTO_DETECT:
            print("ℹ️ AGENT_AUTO_DETECT=false, using fallback agents")


# Serve static files
STATIC_PATH.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_PATH), name="static")

# Serve data attachments
ATTACHMENTS_PATH.mkdir(exist_ok=True)
app.mount("/data/attachments", StaticFiles(directory=ATTACHMENTS_PATH), name="attachments")


@app.get("/")
def read_root():
    """Serve the Kanban UI."""
    return FileResponse(STATIC_PATH / "index.html")


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for live updates."""
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
