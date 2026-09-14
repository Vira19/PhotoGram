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


def test_derniere_ligne_du_env_gagne(tmp_path, monkeypatch):
    """Un reglage ajoute en fin de .env doit primer sur la ligne vide du modele.

    Le modele livre contient les cles avec une valeur vide ; ajouter sa valeur
    a la fin est le geste naturel, et il ne doit pas etre ignore.
    """
    from app.config import load_dotenv

    fichier = tmp_path / ".env"
    fichier.write_text(
        "# commentaire\n"
        "PHOTOGRAM_COLMAP_BIN=\n"
        "PHOTOGRAM_PORT=8000\n"
        "\n"
        "PHOTOGRAM_COLMAP_BIN=C:\\Outils\\colmap\\bin\n"
    )
    for cle in ("PHOTOGRAM_COLMAP_BIN", "PHOTOGRAM_PORT"):
        monkeypatch.delenv(cle, raising=False)

    load_dotenv(fichier)

    import os

    assert os.environ["PHOTOGRAM_COLMAP_BIN"] == "C:\\Outils\\colmap\\bin"
    assert os.environ["PHOTOGRAM_PORT"] == "8000"


def test_variable_environnement_prime_sur_le_fichier(tmp_path, monkeypatch):
    from app.config import load_dotenv

    fichier = tmp_path / ".env"
    fichier.write_text("PHOTOGRAM_PORT=8000\n")
    monkeypatch.setenv("PHOTOGRAM_PORT", "9999")

    load_dotenv(fichier)

    import os

    assert os.environ["PHOTOGRAM_PORT"] == "9999"


def test_env_enregistre_par_le_bloc_notes(tmp_path, monkeypatch):
    """Un .env avec BOM UTF-8 ou en ANSI doit rester lisible.

    Le Bloc-notes de Windows produit couramment l'un ou l'autre ; echouer
    dessus reviendrait a casser le demarrage sur un fichier que l'utilisateur
    vient d'editer tout a fait normalement.
    """
    from app.config import load_dotenv

    avec_bom = tmp_path / "bom.env"
    avec_bom.write_text("# entete\nPHOTOGRAM_COLMAP_BIN=C:\\Outils\\bin\n", encoding="utf-8-sig")
    monkeypatch.delenv("PHOTOGRAM_COLMAP_BIN", raising=False)
    load_dotenv(avec_bom)

    import os

    assert os.environ["PHOTOGRAM_COLMAP_BIN"] == "C:\\Outils\\bin"

    # ANSI avec un accent : illisible en UTF-8 strict.
    ansi = tmp_path / "ansi.env"
    ansi.write_bytes("# r\xe9pertoire\nPHOTOGRAM_PORT=8123\n".encode("cp1252"))
    monkeypatch.delenv("PHOTOGRAM_PORT", raising=False)
    load_dotenv(ansi)

    assert os.environ["PHOTOGRAM_PORT"] == "8123"


def test_cle_avec_bom_en_premiere_ligne(tmp_path, monkeypatch):
    """Sans commentaire d'entete, le BOM colle a la premiere cle."""
    from app.config import load_dotenv

    fichier = tmp_path / "direct.env"
    fichier.write_text("PHOTOGRAM_PORT=7777\n", encoding="utf-8-sig")
    monkeypatch.delenv("PHOTOGRAM_PORT", raising=False)

    load_dotenv(fichier)

    import os

    assert os.environ["PHOTOGRAM_PORT"] == "7777"
