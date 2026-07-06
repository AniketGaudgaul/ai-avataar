"""Structured JSON logging setup.

Section 9 of the spec calls for structured logging; JSON logs make production
traces (Railway / Langfuse) grep-able and machine-parseable from day one.
"""

import logging
import sys

from pythonjsonlogger.json import JsonFormatter

from app.config import settings

_CONFIGURED = False


def setup_logging() -> None:
    """Configure the root logger once, idempotently."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Quiet noisy third-party loggers.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
