# FILE PATH: source_code/router/backend/router.py

import logging
from fastapi import APIRouter
from fastapi.responses import JSONResponse

# Import all route modules
from router.backend.routes.auth_routes import router as auth_router
from router.backend.routes.task_routes import router as task_router
from router.backend.routes.upload_routes import router as upload_router
from router.backend.routes.download_routes import router as download_router

logger = logging.getLogger(__name__)

# -------- Main Router --------

router = APIRouter(
    prefix="",
    tags=["Main Router"],
    responses={404: {"description": "Not found"}},
)

# Include all sub-routers
router.include_router(auth_router)
router.include_router(task_router)
router.include_router(upload_router)  
router.include_router(download_router)

# -------- Direct Processing Endpoint --------
# This one stays here since it's called by background threads, not web requests

from router.backend.routes.processing import elaborate_task

@router.post("/elaborate-task")
async def elaborate_task_endpoint(data: dict):
    """
    Internal processing endpoint called by background threads.
    Not meant for direct web access.
    """
    return await elaborate_task(data)

# -------- Health Check --------

@router.get("/router-health")
def router_health():
    """Simple health check for the router module"""
    return JSONResponse({"status": "healthy", "module": "router"})

logger.info("Router module initialized successfully")