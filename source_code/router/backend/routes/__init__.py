# FILE PATH: source_code/router/backend/routes/__init__.py

"""
Route modules for the vehicle list evaluator application.

This package contains modular FastAPI routers organized by functionality:

- auth_routes: Login, logout, session management
- task_routes: Task listing, task management dashboard  
- upload_routes: File upload handlers (Base64 and multipart)
- download_routes: Processed file download
- processing: Background task processing logic

Each module exports a FastAPI APIRouter that gets included in the main router.
"""

__version__ = "1.0.0"