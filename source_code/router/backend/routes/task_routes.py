# FILE PATH: source_code/router/backend/routes/task_routes.py
# LAMBDA-OPTIMIZED: 5-minute timeout + FIXED supplier dropdown

import os
import threading
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from utils.db_utils import get_database_connection
from routes.auth_routes import get_current_user

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("BASE_URL", "/dev")

router = APIRouter(
    prefix="",
    tags=["Task Routes"],
    responses={404: {"description": "Not found"}},
)

cur_dir = os.path.dirname(__file__)
templates_dir = os.path.join(cur_dir, "..", "templates")
templates = Jinja2Templates(directory=templates_dir)

def _run_elaborate_background(task_id: int):
    """Run elaborate_task in a background thread with its own event loop."""
    import asyncio
    try:
        from routes.processing import elaborate_task
        asyncio.run(elaborate_task({"task_id": task_id}))
    except Exception as e:
        logger.error(f"Processing failed for task {task_id}: {e}")

@router.get("/", response_class=HTMLResponse)
def root(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)

@router.get("/tasks", response_class=HTMLResponse)
def tasks(request: Request, page: int = 1, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    page_size = 20
    offset = (page - 1) * page_size

    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            # LAMBDA-OPTIMIZED: Lightning-fast timeout cleanup (2 lines)
            cutoff_time = datetime.datetime.now() - datetime.timedelta(minutes=5)
            cursor.execute("UPDATE tasks SET status = 'error', message = 'Processing timeout (5 minutes)' WHERE status = 'pending' AND created < %s", (cutoff_time,))
            
            # FIXED: Get suppliers as dictionaries (like original)
            cursor.execute("SELECT supplier_id, supplier_name FROM suppliers ORDER BY supplier_name")
            cols = [d[0] for d in cursor.description]
            suppliers = [dict(zip(cols, row)) for row in cursor.fetchall()]
            
            # Count total tasks
            cursor.execute("SELECT COUNT(*) FROM tasks")
            total_tasks = cursor.fetchone()[0]
            total_pages = (total_tasks + page_size - 1) // page_size

            # Get tasks for current page
            cursor.execute(
                """
                SELECT t.*, s.supplier_name 
                FROM tasks t
                LEFT JOIN suppliers s ON s.supplier_id = t.supplier_id
                ORDER BY t.task_id DESC 
                LIMIT %s OFFSET %s
                """,
                (page_size, offset),
            )
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            tasks = [dict(zip(cols, row)) for row in rows]

            # Check if any task is pending (for auto-refresh)
            at_least_one_pending = any(task["status"] == "pending" for task in tasks)

            conn.commit()  # Commit the timeout updates

    # ZERO APPEARANCE CHANGES - same template response
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "tasks": tasks,
            "at_least_one_pending": at_least_one_pending,
            "suppliers": suppliers,
            "total_pages": total_pages,
            "current_page": page,
            "BASE_URL": BASE_URL,
        },
    )

@router.get("/trigger-elaborate-task")
async def trigger_elaborate_task(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    task_id = int(request.query_params["task_id"])
    t = threading.Thread(target=_run_elaborate_background, args=(task_id,), daemon=True)
    t.start()
    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)