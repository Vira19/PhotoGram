"""Base de donnees : schema, migrations, isolation des ecritures."""

from __future__ import annotations

import sqlite3

from app import db


def test_migration_ajoute_les_colonnes_manquantes(tmp_path, monkeypatch):
    """Une base creee par une version anterieure doit etre completee, pas cassee.

    Le schema s'appuie sur CREATE TABLE IF NOT EXISTS, qui laisse intactes les
    tables existantes : sans migration explicite, une colonne ajoutee plus tard
    ne verrait jamais le jour sur une installation deja en service.
    """
    from app.config import settings

    ancienne = tmp_path / "ancienne.db"
    monkeypatch.setattr(type(settings), "db_path", property(lambda self: ancienne))

    # Base « d'avant » : la table jobs n'a pas encore de colonne attempts.
    conn = sqlite3.connect(ancienne)
    conn.executescript(
        "CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT, description TEXT, "
        "preset TEXT, created_at TEXT);"
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, project_id INTEGER, status TEXT, "
        "preset TEXT, photo_count INTEGER, current_step TEXT, step_index INTEGER, "
        "step_total INTEGER, error TEXT, cancel_requested INTEGER, created_at TEXT, "
        "started_at TEXT, finished_at TEXT);"
        "INSERT INTO jobs (id, project_id, status) VALUES (1, 1, 'done');"
    )
    conn.commit()
    conn.close()

    db.init_db()

    with db.cursor() as conn:
        colonnes = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert "attempts" in colonnes
        # La ligne existante survit, avec la valeur par defaut.
        ligne = conn.execute("SELECT * FROM jobs WHERE id = 1").fetchone()
        assert ligne["status"] == "done" and ligne["attempts"] == 0


def test_init_db_est_idempotent():
    db.init_db()
    db.init_db()
    with db.cursor() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "photos", "jobs", "job_steps", "artifacts"} <= tables


def test_mode_wal_actif():
    """Le web et le worker ecrivent en concurrence : WAL n'est pas optionnel."""
    with db.cursor() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_suppression_projet_cascade():
    """Les cles etrangeres doivent nettoyer photos, jobs et etapes."""
    project_id = db.execute(
        "INSERT INTO projects (name, description, preset, created_at) VALUES ('P', '', 'sparse', ?)",
        (db.now(),),
    )
    db.execute(
        "INSERT INTO photos (project_id, filename, original_name, created_at) VALUES (?, 'a.jpg', 'a.jpg', ?)",
        (project_id, db.now()),
    )
    job_id = db.execute(
        "INSERT INTO jobs (project_id, status, preset, created_at) VALUES (?, 'queued', 'sparse', ?)",
        (project_id, db.now()),
    )
    db.execute(
        "INSERT INTO job_steps (job_id, position, name) VALUES (?, 0, 'etape')", (job_id,)
    )

    db.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    assert db.fetch_all("SELECT * FROM photos WHERE project_id = ?", (project_id,)) == []
    assert db.fetch_all("SELECT * FROM jobs WHERE project_id = ?", (project_id,)) == []
    assert db.fetch_all("SELECT * FROM job_steps WHERE job_id = ?", (job_id,)) == []
