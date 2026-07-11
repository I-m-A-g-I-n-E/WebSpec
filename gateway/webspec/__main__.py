"""Entry point: python -m webspec → uvicorn on [::]:7001."""

import logging
import os
import sys

import uvicorn

from .app import create_app


def main() -> None:
    # WEBSPEC_INTERNAL_PORT is the gateway's listen port (behind Caddy)
    # Falls back to WEBSPEC_PORT for backward compatibility when running without Caddy
    port = int(os.environ.get("WEBSPEC_INTERNAL_PORT", os.environ.get("WEBSPEC_PORT", "7001")))
    log_level = os.environ.get("WEBSPEC_LOG_LEVEL", "info").lower()

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    app = create_app()

    uvicorn.run(
        app,
        host=os.environ.get("WEBSPEC_HOST", "0.0.0.0"),
        port=port,
        log_level=log_level,
    )


if __name__ == "__main__":
    main()
