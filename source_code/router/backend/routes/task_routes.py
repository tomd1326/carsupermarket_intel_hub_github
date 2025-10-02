# FILE PATH: source_code/router/backend/routes/task_routes.py
# LAMBDA-OPTIMIZED: 5-minute timeout + FIXED supplier dropdown + SSE for instant updates

import os
import threading
import datetime
import logging
import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from router.backend.routes.processing import _run_elaborate_background
from router.backend.utils.db_utils import get_database_connection
from router.backend.routes.auth_routes import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Task Management"])
templates = Jinja2Templates(directory="router/backend/templates")

# Global dict to track task completion events
task_events = {}
task_events_lock = threading.Lock()


def notify_task_complete(task_id: int):
    """Notify all SSE listeners that a task is complete."""
    with task_events_lock:
        if task_id in task_events:
            task_events[task_id].set()


@router.get("/tasks", response_class=HTMLResponse)
def get_tasks(request: Request, page: int = 1, username: str = Depends(get_current_user)):
    """Render dashboard with all tasks."""
    BASE_URL = os.getenv("BASE_URL", "/dev")
    TASKS_PER_PAGE = 50
    
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            # Get total count
            cursor.execute("SELECT COUNT(*) FROM tasks")
            total_tasks = cursor.fetchone()[0]
            
            # Get suppliers for dropdown
            cursor.execute("SELECT supplier_id, supplier_name FROM suppliers ORDER BY supplier_name")
            supplier_rows = cursor.fetchall()
            suppliers = [{"supplier_id": r[0], "supplier_name": r[1]} for r in supplier_rows]
            
            # Get paginated tasks
            offset = (page - 1) * TASKS_PER_PAGE
            cursor.execute(
                """
                SELECT t.*, s.supplier_name 
                FROM tasks t
                LEFT JOIN suppliers s ON t.supplier_id = s.supplier_id
                ORDER BY t.created DESC
                LIMIT %s OFFSET %s
                """,
                (TASKS_PER_PAGE, offset)
            )
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            tasks = [dict(zip(cols, r)) for r in rows]

    at_least_one_pending = any(t["status"] in ("pending", "processing") for t in tasks)
    total_pages = (total_tasks + TASKS_PER_PAGE - 1) // TASKS_PER_PAGE

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "tasks": tasks,
            "suppliers": suppliers,
            "at_least_one_pending": at_least_one_pending,
            "username": username,
            "current_page": page,
            "total_pages": total_pages,
            "BASE_URL": BASE_URL,
        },
    )


@router.post("/process/{task_id}")
def process_task(task_id: int, username: str = Depends(get_current_user)):
    """Trigger processing for a specific task."""
    logger.info(f"Processing task {task_id}")
    
    # Create event for SSE notifications
    with task_events_lock:
        task_events[task_id] = threading.Event()
    
    # Start background processing
    thread = threading.Thread(
        target=_run_elaborate_background_with_notification,
        args=(task_id,),
        daemon=True
    )
    thread.start()
    
    return RedirectResponse(url="/tasks", status_code=303)


@router.get("/trigger-elaborate-task")
def trigger_elaborate_task(task_id: int, username: str = Depends(get_current_user)):
    """Trigger re-processing for a task (legacy endpoint)."""
    logger.info(f"Re-processing task {task_id}")
    
    # Create event for SSE notifications
    with task_events_lock:
        task_events[task_id] = threading.Event()
    
    # Start background processing
    thread = threading.Thread(
        target=_run_elaborate_background_with_notification,
        args=(task_id,),
        daemon=True
    )
    thread.start()
    
    return RedirectResponse(url="/tasks", status_code=303)


def _run_elaborate_background_with_notification(task_id: int):
    """Run processing and notify when complete."""
    try:
        _run_elaborate_background(task_id)
    finally:
        # Notify SSE listeners
        notify_task_complete(task_id)
        # Clean up event after 60 seconds
        def cleanup():
            import time
            time.sleep(60)
            with task_events_lock:
                task_events.pop(task_id, None)
        threading.Thread(target=cleanup, daemon=True).start()


@router.get("/task-events/{task_id}")
async def task_events_stream(task_id: int, username: str = Depends(get_current_user)):
    """
    SSE endpoint that streams task status updates.
    Browser connects to this and waits for completion notification.
    """
    async def event_generator():
        # Create event if it doesn't exist
        with task_events_lock:
            if task_id not in task_events:
                task_events[task_id] = threading.Event()
            event = task_events[task_id]
        
        # Check initial status
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT status FROM tasks WHERE task_id = %s", (task_id,))
                row = cursor.fetchone()
                if row and row[0] in ("success", "error"):
                    # Already complete
                    yield f"data: {json.dumps({'status': 'complete'})}\n\n"
                    return
        
        # Wait for completion (with timeout)
        loop = asyncio.get_event_loop()
        try:
            # Wait up to 5 minutes
            await asyncio.wait_for(
                loop.run_in_executor(None, event.wait),
                timeout=300
            )
            # Task completed
            yield f"data: {json.dumps({'status': 'complete'})}\n\n"
        except asyncio.TimeoutError:
            # Timeout - send keepalive
            yield f"data: {json.dumps({'status': 'timeout'})}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )


@router.delete("/tasks/{task_id}")
def delete_task(task_id: int, username: str = Depends(get_current_user)):
    """Delete a task."""
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
            conn.commit()
    return {"message": "Task deleted"}


@router.get("/suppliers")
def get_suppliers(username: str = Depends(get_current_user)):
    """Get list of suppliers for dropdown."""
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT supplier_id, supplier_name FROM suppliers ORDER BY supplier_name")
            rows = cursor.fetchall()
            suppliers = [{"supplier_id": r[0], "supplier_name": r[1]} for r in rows]
    return {"suppliers": suppliers}
