from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.router import v1_router

api_router = APIRouter()

api_router.include_router(v1_router, prefix="/v1")


@api_router.get("/v1/ping", tags=["System"], include_in_schema=False)
async def ping():
    return {"success": True, "message": "pong", "data": None}
