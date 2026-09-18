from __future__ import annotations

import logging
from typing import Any

import structlog

LOG_RENDERER = structlog.dev.ConsoleRenderer()


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(message)s")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            LOG_RENDERER,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "jssg"):
    return structlog.get_logger(name)


def log_error_without_sensitive(
    logger: Any,
    message: str,
    exc: Exception | None = None,
    keys_to_redact: tuple[str, ...] = ("password", "token", "secret", "authorization"),
) -> None:
    if exc is None:
        logger.error(message)
        return

    generic_message = (
        f"{message} (details hidden: ensure no sensitive fields are logged)"
    )
    logger.error(generic_message, error_type=type(exc).__name__)
