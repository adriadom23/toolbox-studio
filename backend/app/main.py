from __future__ import annotations

import uvicorn

from app.core.studio_config import load_studio_config


if __name__ == "__main__":
    config = load_studio_config()
    uvicorn.run(
        "app.asgi:app",
        host=config.host,
        port=config.port,
        proxy_headers=False,
        reload=True,
        reload_dirs=[str(__import__("pathlib").Path(__file__).resolve().parent)],
    )
