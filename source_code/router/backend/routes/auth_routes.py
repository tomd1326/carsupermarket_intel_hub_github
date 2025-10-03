# FILE PATH: source_code/router/backend/routes/auth_routes.py
# FIXED: Template path using pathlib

import os
import pathlib
import hashlib
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

BASE_URL = os.getenv("BASE_URL", "/dev")

# FIXED: Dynamic template path
TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="", tags=["Authentication"])

# -------- Helper Functions --------

def get_current_user(request: Request):
    user = request.session.get("user")
    if not user:
        return None
    return user

# -------- Routes --------

@router.get("/", response_class=HTMLResponse)
def root(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "BASE_URL": BASE_URL})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    if username == "admin" and pwd_hash == "a05ef593239b0ff678c3bb69731e785c470d781a121692302912550aa2459b74":
        request.session["user"] = username
        return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid username or password", "BASE_URL": BASE_URL},
    )


@router.get("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse(url=BASE_URL + "/login", status_code=303)
