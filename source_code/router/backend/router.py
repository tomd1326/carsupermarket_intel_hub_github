import tempfile

from fastapi import APIRouter
from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import FileResponse
from fastapi import File, UploadFile
import os
import hashlib
import logging
import datetime

from utils.s3_tools import upload_file
from utils.db_utils import (get_database_connection)
from typing import Optional


BASE_URL = os.getenv("BASE_URL", "/dev")
router = APIRouter(
    prefix="",
    tags=["Main Router"],
    responses={404: {"description": "Not found"}},
)


cur_dir = os.path.dirname(__file__)
templates_dir = os.path.join(cur_dir, "templates")
templates = Jinja2Templates(directory=templates_dir)

logger = logging.getLogger(__name__)


def get_current_user(request: Request):
    user = request.session.get('user')
    if not user:
        return None
        # raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Unauthorized", headers={"Location": "/login"})
        # redirect to login page
        # return RedirectResponse(url="/login", status_code=303)
    return user


@router.get("/", response_class=HTMLResponse)
def root(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    #
    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)
    # return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})


@router.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, 'BASE_URL': BASE_URL})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    # calculate sha256 hash of the password
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    if username == 'admin' and pwd_hash == "a05ef593239b0ff678c3bb69731e785c470d781a121692302912550aa2459b74":
        request.session['user'] = username
        return RedirectResponse(url=BASE_URL + "/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": "Invalid username or password",
            'BASE_URL': BASE_URL
        }
    )


@router.get("/logout")
def logout(request: Request):
    request.session.pop('user', None)
    return RedirectResponse(url=BASE_URL + "/login", status_code=303)


@router.get("/tasks", response_class=HTMLResponse, name="read_tasks")
def read_tasks(request: Request, page: Optional[int] = 1, page_size: Optional[int] = 25, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    offset = (page - 1) * page_size
    with get_database_connection() as conn:
        # Get the list of suppliers
        with conn.cursor() as cursor:
            cursor.execute("SELECT supplier_id, supplier_name FROM suppliers order by supplier_name")
            cols = [desc[0] for desc in cursor.description]
            suppliers = [dict(zip(cols, row)) for row in cursor.fetchall()]

        # Get the total number of tasks
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM tasks")
            total_tasks = cursor.fetchone()[0]

        # Get the tasks for the current page
        with conn.cursor() as cursor:
            # Adjust the query to use LIMIT and OFFSET for pagination
            cursor.execute("SELECT * FROM tasks ORDER BY task_id DESC LIMIT %s OFFSET %s", (page_size, offset))
            # get as list of dicts
            cols = [desc[0] for desc in cursor.description]
            tasks = [dict(zip(cols, row)) for row in cursor.fetchall()]

    total_pages = (total_tasks + page_size - 1) // page_size  # Calculate the total number of pages

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "tasks": tasks,
        'suppliers': suppliers,
        "total_pages": total_pages,
        "current_page": page,
        'BASE_URL': BASE_URL
    })


@router.post("/upload-task")
async def upload_task(request: Request, file: UploadFile = File(...), user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    with tempfile.NamedTemporaryFile() as temp_file:
        content = await file.read()
        if not content:
            return RedirectResponse(url=BASE_URL + "/", status_code=303)

        filename = file.filename
        file_ext = filename.split('.')[-1]
        if file_ext not in ['csv', 'xls', 'xlsx']:
            return {"message": "Invalid file format. Only csv, xls and xlsx files are allowed."}

        # get the supplider_id from the form
        form = await request.form()
        supplier_id = form.get('supplier_id')
        if not supplier_id:
            return RedirectResponse(url=BASE_URL + "/", status_code=303)

        temp_file.write(content)
        temp_file.seek(0)

        # process the file content
        s3_bucketname = 'aa-alimentation'

        # Create a task in the db
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO tasks (created, status, supplier_id, filename) VALUES (%s, %s, %s, %s)",
                               (datetime.datetime.now(), 'created', supplier_id, file.filename))
                task_id = cursor.lastrowid
                conn.commit()

        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{task_id}.{file_ext}'

        # upload the file to S3
        upload_file(temp_file.name, s3_bucketname, s3_location)

    return RedirectResponse(url=BASE_URL + "/", status_code=303)


@router.get("/download/{filename}", response_class=FileResponse)
def download_file(filename: str):
    return FileResponse(path=f"some/directory/{filename}", filename=filename)
