"""The shared Jinja environment for the web UI and the auth pages."""

import hashlib
from functools import cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
templates = Jinja2Templates(directory=HERE / "templates")


@cache
def static_url(name: str) -> str:
    """URL for a static file, versioned by its content so a release never reuses a stale cache."""
    digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
    return f"/static/{name}?v={digest}"


templates.env.globals["static_url"] = static_url
