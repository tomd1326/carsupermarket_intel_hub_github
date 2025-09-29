# FILE PATH: source_code/router/backend/routes/task_routes.py

import os
import threading
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from utils.db_utils import get_database_connection
from .processing import _run_elaborate_background  # Import from processing module
from .auth_routes import get_current_user  # Import from auth module

BASE_URL = os.getenv("BASE_URL", "/dev")

# Set up templates
cur_dir = os.path.dirname(__file__)
templates_dir = os.path.join(cur_dir, "../templates")
templates = Jinja2Templates(directory=templates_dir)

router = APIRouter(prefix="", tags=["Task Management"])

# -------- Routes --------

@router.get("/tasks", response_class=HTMLResponse, name="read_tasks")
def read_tasks(
    request: Request,
    page: Optional[int] = 1,
    page_size: Optional[int] = 25,
    user: str = Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    at_least_one_pending = False
    offset = (page - 1) * page_size

    with get_database_connection() as conn:
        # Suppliers
        with conn.cursor() as cursor:
            cursor.execute("SELECT supplier_id, supplier_name FROM suppliers ORDER BY supplier_name")
            cols = [d[0] for d in cursor.description]
            suppliers = [dict(zip(cols, row)) for row in cursor.fetchall()]

        # Count
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM tasks")
            total_tasks = cursor.fetchone()[0]

        # Page
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM tasks t
                LEFT JOIN suppliers s ON t.supplier_id = s.supplier_id
                ORDER BY t.task_id DESC
                LIMIT %s OFFSET %s
                """,
                (page_size, offset),
            )
            cols = [d[0] for d in cursor.description]
            tasks = []
            for row in cursor.fetchall():
                task = dict(zip(cols, row))
                tasks.append(task)
                if task["status"] == "pending":
                    at_least_one_pending = True

    total_pages = (total_tasks + page_size - 1) // page_size

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