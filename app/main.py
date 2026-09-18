from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.config import get_settings
from app.core.exceptions import AppException
from app.core.responses import error_response
from app.database import async_session_factory
from app.services.bootstrap_service import bootstrap_app

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bootstrap_app(async_session_factory)
    yield


app = FastAPI(
    title=settings.app_name,
    description=(
        "JSSG - Centralized Jain Community Digital Platform. "
        "REST API consumed by the Flutter mobile app and the web admin panel."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(
            message=str(exc.detail),
            error_code=exc.error_code,
            status_code=exc.status_code,
        )["content"],
    )


@app.get("/", include_in_schema=False)
async def root():
    return {"app": settings.app_name, "message": "JSSG API is running"}


@app.get("/health", tags=["health"])
async def health_check():
    return {
        "success": True,
        "message": "Healthy",
        "data": {
            "app": settings.app_name,
            "environment": settings.app_env,
        },
    }


app.include_router(api_router, prefix="/api")
