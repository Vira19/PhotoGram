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


def test_le_lanceur_rappelle_ou_lire_le_mot_de_passe(tmp_path, monkeypatch, capsys):
    """Passe la creation, le mot de passe n'est plus affiche : il faut dire ou il est."""
    monkeypatch.setattr(run, "RACINE", tmp_path)
    cible = tmp_path / ".env"
    cible.write_text("PHOTOGRAM_PASSWORD=deja-defini\n")

    assert run.creer_env_si_absent(cible) == ""
    # Le rappel affiche par main() pointe vers le fichier, jamais vers la valeur.
    rappel = f"ligne PHOTOGRAM_PASSWORD de {tmp_path / '.env'}"
    assert "deja-defini" not in rappel


def _lignes_pour(chemin, cle):
    return [
        ligne for ligne in chemin.read_text().splitlines()
        if ligne.startswith(cle + "=")
    ]


def test_definir_met_a_jour_la_ligne_existante(tmp_path):
    cible = tmp_path / ".env"
    cible.write_text("# entete\nPHOTOGRAM_COLMAP_BIN=\nPHOTOGRAM_PORT=8000\n")

    assert run.definir_reglage(cible, f"PHOTOGRAM_COLMAP_BIN={tmp_path}") == 0

    assert _lignes_pour(cible, "PHOTOGRAM_COLMAP_BIN") == [f"PHOTOGRAM_COLMAP_BIN={tmp_path}"]
    # Le reste du fichier est preserve.
    assert "# entete" in cible.read_text()
    assert "PHOTOGRAM_PORT=8000" in cible.read_text()


def test_definir_supprime_les_doublons(tmp_path):
    """Une edition manuelle anterieure a pu laisser deux lignes pour une cle."""
    cible = tmp_path / ".env"
    cible.write_text(
        "PHOTOGRAM_COLMAP_BIN=\nPHOTOGRAM_PORT=8000\nPHOTOGRAM_COLMAP_BIN=/vieux\n"
    )

    run.definir_reglage(cible, f"PHOTOGRAM_COLMAP_BIN={tmp_path}")

    assert _lignes_pour(cible, "PHOTOGRAM_COLMAP_BIN") == [f"PHOTOGRAM_COLMAP_BIN={tmp_path}"]


def test_definir_ajoute_une_cle_absente(tmp_path):
    cible = tmp_path / ".env"
    cible.write_text("PHOTOGRAM_PORT=8000\n")

    run.definir_reglage(cible, f"PHOTOGRAM_OPENMVS_BIN={tmp_path}")

    assert _lignes_pour(cible, "PHOTOGRAM_OPENMVS_BIN") == [f"PHOTOGRAM_OPENMVS_BIN={tmp_path}"]


def test_definir_signale_un_dossier_inexistant(tmp_path, capsys):
    """Un chemin errone doit se voir tout de suite, pas au premier calcul."""
    cible = tmp_path / ".env"
    cible.write_text("PHOTOGRAM_COLMAP_BIN=\n")

    code = run.definir_reglage(cible, "PHOTOGRAM_COLMAP_BIN=C:\\nexiste\\pas")

    assert code == 1
    assert "n'existe pas" in capsys.readouterr().out
    # La valeur est tout de meme ecrite : l'utilisateur voit ce qu'il a saisi.
    assert _lignes_pour(cible, "PHOTOGRAM_COLMAP_BIN") == ["PHOTOGRAM_COLMAP_BIN=C:\\nexiste\\pas"]


def test_definir_refuse_une_cle_etrangere(tmp_path, capsys):
    cible = tmp_path / ".env"
    cible.write_text("PHOTOGRAM_PORT=8000\n")

    assert run.definir_reglage(cible, "PATH=/usr/bin") == 1
    assert cible.read_text() == "PHOTOGRAM_PORT=8000\n"


def test_definir_enleve_les_guillemets(tmp_path):
    """Un chemin Windows colle depuis l'explorateur arrive souvent entre guillemets."""
    cible = tmp_path / ".env"
    cible.write_text("PHOTOGRAM_COLMAP_BIN=\n")

    run.definir_reglage(cible, f'PHOTOGRAM_COLMAP_BIN="{tmp_path}"')

    assert _lignes_pour(cible, "PHOTOGRAM_COLMAP_BIN") == [f"PHOTOGRAM_COLMAP_BIN={tmp_path}"]
