"""Lanceur local (python -m app.run)."""

from __future__ import annotations

from app import run


def test_reglages_suivent_la_memoire(monkeypatch):
    """Les defauts du projet visent 1 Go : une grosse machine doit etre deliee."""
    monkeypatch.setattr(run, "_memoire_totale_mo", lambda: 32000)
    monkeypatch.setattr(run.os, "cpu_count", lambda: 8)
    gros = run.reglages_pour_cette_machine()
    assert gros["work_max_dim"] >= 3200
    assert gros["threads"] == 7  # un coeur laisse a l'interface

    monkeypatch.setattr(run, "_memoire_totale_mo", lambda: 900)
    petit = run.reglages_pour_cette_machine()
    assert petit["work_max_dim"] == 1600
    assert petit["max_photos"] == 40
    assert petit["threads"] <= 3


def test_creation_du_env(tmp_path, monkeypatch):
    # 10 Go : palier « machine de bureau », entre le Raspberry Pi et la station.
    monkeypatch.setattr(run, "_memoire_totale_mo", lambda: 10000)
    cible = tmp_path / ".env"

    mot_de_passe = run.creer_env_si_absent(cible)

    assert mot_de_passe and len(mot_de_passe) >= 8
    contenu = dict(
        ligne.split("=", 1)
        for ligne in cible.read_text().splitlines()
        if "=" in ligne and not ligne.startswith("#")
    )
    assert contenu["PHOTOGRAM_PASSWORD"] == mot_de_passe
    assert len(contenu["PHOTOGRAM_SECRET_KEY"]) == 64
    assert contenu["PHOTOGRAM_DATA_DIR"].endswith("data")
    # Les reglages doivent refleter la machine, pas les defauts Raspberry Pi.
    assert contenu["PHOTOGRAM_WORK_MAX_DIM"] == "2400"

    # Les commentaires du modele sont conserves : le fichier reste lisible.
    assert "# --- PhotoGram" in cible.read_text()


def test_env_existant_jamais_ecrase(tmp_path):
    cible = tmp_path / ".env"
    cible.write_text("PHOTOGRAM_PASSWORD=le-mien\n")

    assert run.creer_env_si_absent(cible) == ""
    assert cible.read_text() == "PHOTOGRAM_PASSWORD=le-mien\n"


def test_adresses_locales():
    adresses = run.adresses_locales(8000)
    assert "http://127.0.0.1:8000" in adresses
    assert all(a.startswith("http://") and a.endswith(":8000") for a in adresses)
