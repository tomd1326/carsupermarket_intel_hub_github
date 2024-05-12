import base64
import tempfile
import threading
import traceback
from multiprocessing.pool import ThreadPool

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

from utils.lambda_http import LambdaRequestsManager
from utils.s3_tools import upload_file, download_file
from utils.db_utils import (get_database_connection)
from list_processor.autotrader_client import AutoTraderClient
from list_processor.cap_client import CAPClient
from utils.secrets_manager_client import get_secret
from utils.file_handler import read_dataframe_file, write_dataframe_file
from typing import Optional
import pandas as pd


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

# Generic initialization
s3_bucketname = 'aa-alimentation'


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
        return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)
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
            cursor.execute("SELECT * FROM tasks t LEFT JOIN suppliers s ON t.supplier_id = s.supplier_id ORDER BY t.task_id DESC LIMIT %s OFFSET %s", (page_size, offset))
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
async def upload_task(request: Request, fileBase64: str = Form(...), supplier_id: str = Form(...), filename: str = Form(...), user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    fd_input, temp_input_file_path = tempfile.mkstemp()
    os.close(fd_input)
    try:
        # Decode the base64 string to binary data
        content = base64.b64decode(fileBase64)
        print(f'File size from the form: {len(content)}')
        if not content:
            return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)

        file_ext = filename.split('.')[-1]
        if file_ext not in ['csv', 'xls', 'xlsx']:
            return {"message": "Invalid file format. Only csv, xls and xlsx files are allowed."}

        # get the supplider_id from the form
        form = await request.form()
        supplier_id = form.get('supplier_id')
        if not supplier_id:
            return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)

        with open(temp_input_file_path, 'wb') as temp_file:
            temp_file.write(content)

        # Create a task in the db
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO tasks (created, status, supplier_id, filename) VALUES (%s, %s, %s, %s)",
                               (datetime.datetime.now(), 'created', supplier_id, filename))
                task_id = cursor.lastrowid
                conn.commit()

        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{task_id}.{file_ext}'

        # upload the file to S3
        upload_file(temp_input_file_path, s3_bucketname, s3_location)

        # Execute the task elaboration
        LambdaRequestsManager().simulate_http(
            # lambda_requests_manager.simulate_http(
            aws_context=request.scope["aws.context"] if "aws.context" in request.scope else None,
            path=BASE_URL + "/elaborate-task",
            method="POST",
            data={
                'task_id': task_id,
            }
        )
    finally:
        os.remove(temp_input_file_path)

    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)


@router.get("/download/{task_id}", response_class=FileResponse)
def download_task_file(task_id: str):
    # Get task info from the db
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM tasks WHERE task_id = %s", (task_id,))
            cols = [desc[0] for desc in cursor.description]
            task = dict(zip(cols, cursor.fetchone()))

    if not task:
        return {"message": "Task not found"}

    with tempfile.NamedTemporaryFile(suffix='.' + task['filename'].split('.')[-1], delete=False) as temp_output_file:
        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        file_ext = task['filename'].split('.')[-1]
        s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{task["task_id"]}.{file_ext}'
        download_file(temp_output_file.name, s3_bucketname, s3_location)
        # Add the suffix _output to the filename
        output_filename = task['filename'].split('.')[0] + '_output.' + task['filename'].split('.')[-1]
        return FileResponse(temp_output_file.name, filename=output_filename)  #, media_type='application/octet-stream')


@router.post("/elaborate-task")
async def elaborate_task(data: dict):
    # Fetch the task data from the db
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM tasks WHERE task_id = %s", (data['task_id'],))
            cols = [desc[0] for desc in cursor.description]  # Get column names from cursor.description
            row = cursor.fetchone()
            task = dict(zip(cols, row)) if row else None
            if not task:
                return {"message": "Task not found"}

            # Update the task status
            cursor.execute("UPDATE tasks SET status = 'pending' WHERE task_id = %s", (data['task_id'],))
            conn.commit()

    # Download the file from S3
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    file_ext = task['filename'].split('.')[-1]
    s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{data["task_id"]}.{file_ext}'
    with tempfile.NamedTemporaryFile(suffix='.' + file_ext) as temp_input_file, \
         tempfile.NamedTemporaryFile(suffix='.' + file_ext) as temp_output_file:
        download_file(temp_input_file.name, s3_bucketname, s3_location)

        secrets = get_secret(f'{env}/carsupermarket/car_pricer/api_access')
        # Elaborate the file
        try:
            input_df = read_dataframe_file(temp_input_file.name)

            autotrader = AutoTraderClient()
            cap_client = CAPClient()

            def process_df(args):
                processor_obj, input_df = args
                return processor_obj.process_file(input_df)

            tp = ThreadPool(processes=2)
            inputs = [(autotrader, input_df), (cap_client, input_df)]
            results = tp.map(process_df, inputs)

            processed_df = pd.concat([input_df, *results], axis=1)

            write_dataframe_file(processed_df, temp_output_file.name)

            # Upload the elaborated file to S3
            s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{data["task_id"]}.{file_ext}'
            upload_file(temp_output_file.name, s3_bucketname, s3_location)

            status = 'success'
            message = None
        except Exception as e:
            logger.error(f"Error processing file: {e}, traceback: {traceback.format_exc()}")
            status = 'error'
            message = str(e)

        # Update the task status
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE tasks SET status = %s, message = %s WHERE task_id = %s", (status, message, data['task_id'],))
                conn.commit()

    return {}
