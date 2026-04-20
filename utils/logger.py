"""
Quil Logger

Structured logging with structlog for clean, parseable output across
all modules. Logs include timestamps, module names, and severity levels.
"""

import sys
import logging
import structlog
from pathlib import Path
from config.settings import BASE_DIR


LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)


def setup_logging(level: str = "INFO") -> None:
    """Configure structured logging for the entire application."""

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    file_handler = logging.FileHandler(LOG_DIR / "quil.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(file_handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """Get a named logger instance."""
    return structlog.get_logger(name)
