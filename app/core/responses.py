from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class SuccessResponse(BaseModel, Generic[T]):
    success: bool = True
    message: str
    data: T | None = None


class PaginationMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class PaginatedResponse(BaseModel, Generic[T]):
    success: bool = True
    message: str
    data: list[T]
    pagination: PaginationMeta


class ErrorResponse(BaseModel):
    success: bool = False
    message: str
    error_code: str | None = None


def success_response(
    message: str,
    data: Any = None,
    status_code: int = 200,
) -> dict[str, Any]:
    content = {
        "success": True,
        "message": message,
    }
    if data is not None:
        content["data"] = data
    return {
        "status_code": status_code,
        "content": content,
    }


def ok(message: str, data: Any = None) -> dict[str, Any]:
    return success_response(message, data)["content"]


def created(message: str, data: Any = None) -> dict[str, Any]:
    return success_response(message, data, 201)["content"]


def paginated_response(
    message: str,
    items: list[Any],
    pagination: dict[str, Any],
    status_code: int = 200,
) -> dict[str, Any]:
    return {
        "status_code": status_code,
        "content": {
            "success": True,
            "message": message,
            "data": items,
            "pagination": pagination,
        },
    }


def paginated(
    message: str, items: list[Any], pagination: dict[str, Any]
) -> dict[str, Any]:
    return paginated_response(message, items, pagination)["content"]


def error_response(
    message: str,
    error_code: str | None = None,
    status_code: int = 400,
) -> dict[str, Any]:
    return {
        "status_code": status_code,
        "content": {
            "success": False,
            "message": message,
            "error_code": error_code,
        },
    }
