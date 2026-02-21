"""
Project CRUD endpoints.
"""

import logging
import re
from typing import List
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException

from app.database import get_db, get_db_write
from app.models import ProjectCreate, ProjectResponse
from app.websocket import manager

logger = logging.getLogger(__name__)
router = APIRouter()


def _slugify(name: str) -> str:
    """Generate a URL-safe slug from a project name."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug)
    return slug.strip("-")


@router.get("/api/projects", response_model=List[ProjectResponse])
def list_projects():
    """Return all projects ordered by id (Default first)."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY id").fetchall()
        return [dict(row) for row in rows]


@router.post("/api/projects", response_model=ProjectResponse)
async def create_project(project: ProjectCreate):
    """Create a new project."""
    slug = _slugify(project.name)
    if not slug:
        raise HTTPException(status_code=400, detail="Project name produces an empty slug")
    now = datetime.now(timezone.utc).isoformat()
    with get_db_write() as conn:
        # Check for duplicate slug
        existing = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Project with slug '{slug}' already exists")
        cursor = conn.execute(
            "INSERT INTO projects (name, slug, description, color, created_at) VALUES (?, ?, ?, ?, ?)",
            (project.name, slug, project.description, project.color, now)
        )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (cursor.lastrowid,)).fetchone()
        result = dict(row)
    logger.info(f"Project created: {project.name} (slug={slug})")
    await manager.broadcast({"type": "project_created", "project": result})
    return result


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: int):
    """Delete a project. Cannot delete Default (id=1). Reassigns tasks to Default."""
    if project_id == 1:
        raise HTTPException(status_code=400, detail="Cannot delete the Default project")
    with get_db_write() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        # Reassign tasks to Default project
        conn.execute("UPDATE tasks SET project_id = 1 WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    logger.info(f"Project #{project_id} deleted: {row['name']}")
    await manager.broadcast({"type": "project_deleted", "project_id": project_id})
    return {"status": "deleted", "id": project_id}
