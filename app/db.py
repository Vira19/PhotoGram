"""Acces SQLite.

Le web et le worker sont deux processus distincts qui partagent la meme base :
le mode WAL est donc obligatoire, et chaque ecriture passe par une transaction
courte pour ne jamais bloquer l'autre processus.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    preset      TEXT NOT NULL DEFAULT 'rpi',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    original_name TEXT NOT NULL,
    width         INTEGER NOT NULL DEFAULT 0,
    height        INTEGER NOT NULL DEFAULT 0,
    bytes         INTEGER NOT NULL DEFAULT 0,
    sharpness     REAL,
    focal_mm      REAL,
    camera        TEXT NOT NULL DEFAULT '',
    taken_at      TEXT NOT NULL DEFAULT '',
    included      INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    UNIQUE (project_id, filename)
);

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'queued',
    preset           TEXT NOT NULL DEFAULT 'rpi',
    photo_count      INTEGER NOT NULL DEFAULT 0,
    current_step     TEXT NOT NULL DEFAULT '',
    step_index       INTEGER NOT NULL DEFAULT 0,
    step_total       INTEGER NOT NULL DEFAULT 0,
    error            TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT
);

CREATE TABLE IF NOT EXISTS job_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    exit_code   INTEGER,
    duration_s  REAL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    filename   TEXT NOT NULL,
    bytes      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_photos_project ON photos(project_id);
CREATE INDEX IF NOT EXISTS idx_jobs_project   ON jobs(project_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_steps_job      ON job_steps(job_id, position);
CREATE INDEX IF NOT EXISTS idx_artifacts_job  ON artifacts(job_id);
"""


def now() -> str:
    """Horodatage UTC ISO 8601, seconde pres."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # La carte SD d'un RPi est lente : un cache un peu plus large evite
    # beaucoup de lectures, sans peser sur la RAM (2 Mo).
    conn.execute("PRAGMA cache_size=-2000")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Transaction immediate : le verrou d'ecriture est pris d'entree.

    Indispensable pour que le worker puisse reclamer un job sans qu'une
    requete concurrente du web ne s'intercale.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


#: Colonnes ajoutees apres coup : (table, colonne, definition).
#: Le schema utilise CREATE TABLE IF NOT EXISTS, qui ne touche pas aux tables
#: deja creees ; une base existante doit donc etre completee explicitement.
MIGRATIONS = [
    ("jobs", "attempts", "INTEGER NOT NULL DEFAULT 0"),
]


def init_db() -> None:
    with cursor() as conn:
        conn.executescript(SCHEMA)
        for table, colonne, definition in MIGRATIONS:
            existantes = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if colonne not in existantes:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} {definition}")


def fetch_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    with cursor() as conn:
        return conn.execute(sql, params).fetchone()


def fetch_all(sql: str, params: tuple = ()) -> list:
    with cursor() as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql: str, params: tuple = ()) -> int:
    """Execute une ecriture et renvoie le lastrowid."""
    with cursor() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid
