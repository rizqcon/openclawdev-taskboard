"""
WebSocket connection manager and broadcast helper.
"""

from typing import Set
from fastapi import WebSocket


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
