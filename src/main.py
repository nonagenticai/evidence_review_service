"""Uvicorn entrypoint for Evidence Review 8810."""

from __future__ import annotations

import os

import uvicorn

from app import create_app
from config import Settings, get_settings
from log_config import configure_logging, get_logger

logger = get_logger(__name__)

app = create_app()


def run_uvicorn(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    log_dir = "/app/logs" if os.path.exists("/app/logs") else os.path.expanduser("~/Movies/.logs")
    os.makedirs(log_dir, exist_ok=True)
    configure_logging(log_file=os.path.join(log_dir, "evidence_review.log"))
    logger.info("starting evidence-review", extra={"host": settings.host, "port": settings.port})
    uvicorn.run("app:create_app", factory=True, host=settings.host, port=settings.port, loop="asyncio")


def main() -> None:
    run_uvicorn()


if __name__ == "__main__":
    main()
