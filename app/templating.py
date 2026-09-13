"""Environnement Jinja2 partage par toutes les routes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import __version__
from .system import human_bytes

TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def format_datetime(value) -> str:
    """Rend un horodatage ISO stocke en base sous forme lisible."""
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return moment.astimezone().strftime("%d/%m/%Y %H:%M")


def format_duration(seconds) -> str:
    if not seconds:
        return "—"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


STATUS_LABELS = {
    "queued": "En attente",
    "running": "En cours",
    "done": "Termine",
    "failed": "Echec",
    "cancelled": "Annule",
    "pending": "En attente",
    "skipped": "Ignore",
}

templates.env.filters["bytes"] = human_bytes
templates.env.filters["datetime"] = format_datetime
templates.env.filters["duration"] = format_duration
templates.env.globals["status_label"] = lambda key: STATUS_LABELS.get(key, key)
templates.env.globals["app_version"] = __version__
