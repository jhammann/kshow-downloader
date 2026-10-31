"""The shared Jinja environment for the web UI and the auth pages."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
templates = Jinja2Templates(directory=HERE / "templates")
