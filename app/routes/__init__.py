"""
Collects all APIRouters into a single api_router.
"""

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
