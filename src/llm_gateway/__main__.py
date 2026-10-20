"""``python -m llm_gateway`` / ``llm-gateway`` entrypoint."""

from __future__ import annotations

import uvicorn

from llm_gateway.app import create_app
from llm_gateway.config import Settings
from llm_gateway.observability import configure_logging


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    app = create_app(settings=settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        proxy_headers=True,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
