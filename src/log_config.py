"""
Structured logging with OpenTelemetry trace correlation.
"""

import logging
import os
import sys
from typing import Any, cast

import structlog

# Check if OTEL is available
OTEL_AVAILABLE = False
trace: Any = None
try:
    from opentelemetry import trace

    OTEL_AVAILABLE = True
except ImportError:
    pass


def add_trace_context(logger, method_name, event_dict):
    """Inject OpenTelemetry trace context into structured logs."""
    if OTEL_AVAILABLE and trace:
        span = trace.get_current_span()
        if span and span.is_recording():
            ctx = span.get_span_context()
            if ctx.is_valid:
                event_dict["trace_id"] = format(ctx.trace_id, "032x")
                event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


#: The default `ENVIRONMENT`, kept identical to `config.Settings`.
#:
#: These were two defaults for one fact and they disagreed: `Settings` said
#: `production`, this module said `development`. `ENVIRONMENT` is unset on the
#: deployment, so every log line the production service emitted was stamped
#: `environment: development` — an operator filtering their log store for
#: `environment=production` saw nothing at all from this service, and would have
#: concluded it was quiet rather than mislabelled.
#:
#: It is not imported from `config` on purpose: `log_config` is initialised
#: before `get_settings()` in `main.py`, and importing settings here would make
#: logging depend on configuration parsing succeeding — so a bad env var would
#: cost the log line that explains it. The value is duplicated, and
#: `tests/unit/test_log_config.py` asserts the two agree.
DEFAULT_ENVIRONMENT = "production"


def add_service_context(logger, method_name, event_dict):
    """Add service metadata to logs."""
    event_dict["service"] = os.getenv("OTEL_SERVICE_NAME", "evidence-review")
    event_dict["environment"] = os.getenv("ENVIRONMENT", DEFAULT_ENVIRONMENT)
    return event_dict


_configured = False


def configure_logging(level: int | None = None, log_file: str | None = None) -> None:
    """Configure structured JSON logging with trace context.

    Args:
        level: Logging level (defaults to INFO or LOG_LEVEL env var)
        log_file: Optional file path for logging (ignored in structlog setup)
    """
    global _configured

    if _configured:
        return

    log_format = os.getenv("LOG_FORMAT", "json").lower()
    log_level = level or getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper())

    # Determine processors based on format
    if log_format == "json":
        processors = [
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            add_service_context,
            add_trace_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Human-readable console format for development
        processors = [
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            add_service_context,
            add_trace_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure stdlib logging
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)

    # Suppress noisy loggers
    for logger_name in ["httpx", "httpcore", "uvicorn.access", "urllib3"]:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a module-level structured logger.

    Args:
        name: Logger name (defaults to "evidence-review")

    Returns:
        Configured structlog logger
    """
    configure_logging()
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name or "evidence-review"))
