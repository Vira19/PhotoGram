"""Configuration lue depuis l'environnement (voir .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "oui"}


def load_dotenv(path: Path) -> None:
    """Charge un .env minimaliste sans ecraser l'environnement existant.

    On evite une dependance supplementaire : le format supporte est
    ``CLE=valeur``, les lignes vides et les commentaires ``#``.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    password: str = ""
    secret_key: str = ""
    data_dir: Path = field(default_factory=lambda: Path("data"))
    host: str = "0.0.0.0"
    port: int = 8000

    # Garde-fous RPi3
    work_max_dim: int = 1600
    max_photos: int = 40
    max_upload_mb: int = 40
    threads: int = 3
    keep_intermediates: bool = False

    # Chaine de reconstruction : "auto", "openmvg" ou "colmap"
    backend: str = "auto"

    # Binaires
    openmvg_bin: str = ""
    openmvs_bin: str = ""
    colmap_bin: str = ""
    sensor_db: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parent.parent
        load_dotenv(root / ".env")
        return cls(
            password=_env("PHOTOGRAM_PASSWORD"),
            secret_key=_env("PHOTOGRAM_SECRET_KEY"),
            data_dir=Path(_env("PHOTOGRAM_DATA_DIR", str(root / "data"))).expanduser(),
            host=_env("PHOTOGRAM_HOST", "0.0.0.0"),
            port=_env_int("PHOTOGRAM_PORT", 8000),
            work_max_dim=_env_int("PHOTOGRAM_WORK_MAX_DIM", 1600),
            max_photos=_env_int("PHOTOGRAM_MAX_PHOTOS", 40),
            max_upload_mb=_env_int("PHOTOGRAM_MAX_UPLOAD_MB", 40),
            threads=max(1, _env_int("PHOTOGRAM_THREADS", 3)),
            keep_intermediates=_env_bool("PHOTOGRAM_KEEP_INTERMEDIATES", False),
            backend=_env("PHOTOGRAM_BACKEND", "auto").lower() or "auto",
            openmvg_bin=_env("PHOTOGRAM_OPENMVG_BIN"),
            openmvs_bin=_env("PHOTOGRAM_OPENMVS_BIN"),
            colmap_bin=_env("PHOTOGRAM_COLMAP_BIN"),
            sensor_db=_env("PHOTOGRAM_SENSOR_DB"),
        )

    # --- Chemins ---

    @property
    def db_path(self) -> Path:
        return self.data_dir / "photogram.db"

    def project_dir(self, project_id: int) -> Path:
        return self.data_dir / "projects" / str(project_id)

    def photos_dir(self, project_id: int) -> Path:
        return self.project_dir(project_id) / "photos"

    def thumbs_dir(self, project_id: int) -> Path:
        return self.project_dir(project_id) / "thumbs"

    def work_dir(self, project_id: int) -> Path:
        return self.project_dir(project_id) / "work"

    def job_dir(self, project_id: int, job_id: int) -> Path:
        return self.project_dir(project_id) / "jobs" / str(job_id)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "projects").mkdir(parents=True, exist_ok=True)

    def problems(self) -> list:
        """Erreurs de configuration bloquantes, listees pour /health."""
        issues = []
        if not self.password:
            issues.append("PHOTOGRAM_PASSWORD n'est pas defini : personne ne peut se connecter.")
        if not self.secret_key:
            issues.append("PHOTOGRAM_SECRET_KEY n'est pas defini : les sessions ne sont pas signees.")
        return issues


settings = Settings.from_env()
