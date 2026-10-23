from __future__ import annotations

import uvicorn

from app.core.studio_config import load_studio_config
from app.preview_app import create_preview_app


def main() -> None:
    config = load_studio_config()
    uvicorn.run(create_preview_app(config=config), host=config.preview_host, port=config.preview_port, proxy_headers=False)


if __name__ == "__main__":
    main()
