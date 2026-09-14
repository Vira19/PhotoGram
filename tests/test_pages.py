"""Rendu des gabarits.

Une erreur Jinja ne se voit qu'a l'execution : ces tests parcourent chaque page
apres une reconstruction reussie, seule facon de verifier les branches qui
n'existent qu'une fois des resultats produits.
"""

from __future__ import annotations

import pytest

from app import db, worker
from app.config import settings


@pytest.fixture
def projet_reconstruit(client_connecte, photo_jpeg, chaine_factice):
    """Un projet avec 6 photos et une reconstruction complete menee a son terme."""
    reponse = client_connecte.post(
        "/projects", data={"name": "Sujet complet", "description": "Notes", "preset": "rpi"},
        follow_redirects=False,
    )
    project_id = int(reponse.headers["location"].rsplit("/", 1)[1])
    client_connecte.post(
        f"/projects/{project_id}/photos",
        files=[("files", (f"IMG_{i}.jpg", photo_jpeg(graine=i), "image/jpeg")) for i in range(6)],
        follow_redirects=False,
    )
    job_id = db.execute(
        "INSERT INTO jobs (project_id, status, preset, photo_count, created_at) "
        "VALUES (?, 'queued', 'rpi', 6, ?)",
        (project_id, db.now()),
    )
    worker.process_job(worker.claim_job())
    assert db.fetch_one("SELECT status FROM jobs WHERE id = ?", (job_id,))["status"] == "done"
    return project_id, job_id


def test_page_accueil(client_connecte, projet_reconstruit):
    reponse = client_connecte.get("/")
    assert reponse.status_code == 200
    assert "Sujet complet" in reponse.text
    assert "RAM libre" in reponse.text


def test_page_projet(client_connecte, projet_reconstruit):
    project_id, _ = projet_reconstruit
    reponse = client_connecte.get(f"/projects/{project_id}")
    assert reponse.status_code == 200
    assert "Conseils de prise de vue" in reponse.text
    assert "Photos (6)" in reponse.text
    assert "Ecarter" in reponse.text


def test_page_job_et_resultats(client_connecte, projet_reconstruit):
    _, job_id = projet_reconstruit
    reponse = client_connecte.get(f"/jobs/{job_id}")
    assert reponse.status_code == 200
    assert "texture.obj" in reponse.text
    assert "Ouvrir la visionneuse 3D" in reponse.text
    assert "Termine" in reponse.text


def test_fragment_etat(client_connecte, projet_reconstruit):
    _, job_id = projet_reconstruit
    reponse = client_connecte.get(f"/jobs/{job_id}/etat")
    assert reponse.status_code == 200
    # Le marqueur de fin dit au client d'arreter de solliciter le serveur.
    assert "data-termine" in reponse.text
    assert "Collecte des resultats" in reponse.text


def test_journal(client_connecte, projet_reconstruit):
    _, job_id = projet_reconstruit
    reponse = client_connecte.get(f"/jobs/{job_id}/journal")
    assert reponse.status_code == 200
    assert "Reconstruction terminee avec succes" in reponse.text


def test_journal_tronque_aux_dernieres_lignes(client_connecte, projet_reconstruit):
    """Un journal enorme ne doit pas partir en entier sur le reseau."""
    from app.routes.jobs import LOG_TAIL_BYTES

    project_id, job_id = projet_reconstruit
    chemin = settings.job_dir(project_id, job_id) / "job.log"
    with open(chemin, "a", encoding="utf-8") as handle:
        handle.write("ligne de remplissage\n" * 6000)

    reponse = client_connecte.get(f"/jobs/{job_id}/journal")
    assert len(reponse.text) < LOG_TAIL_BYTES + 200
    assert "octets plus anciens omis" in reponse.text


def test_telechargement_resultat(client_connecte, projet_reconstruit):
    _, job_id = projet_reconstruit
    reponse = client_connecte.get(f"/jobs/{job_id}/fichiers/texture.obj")
    assert reponse.status_code == 200
    assert "attachment" in reponse.headers["content-disposition"]
    assert reponse.text.startswith("o maillage")


def test_visionneuse(client_connecte, projet_reconstruit):
    _, job_id = projet_reconstruit
    reponse = client_connecte.get(f"/jobs/{job_id}/visionneuse")
    assert reponse.status_code == 200
    assert "viewer.js" in reponse.text
    assert "PhotoGramViewer.charger" in reponse.text


def test_visionneuse_choix_du_fichier(client_connecte, projet_reconstruit):
    _, job_id = projet_reconstruit
    reponse = client_connecte.get(f"/jobs/{job_id}/visionneuse", params={"fichier": "dense.ply"})
    assert reponse.status_code == 200
    assert "/fichiers/dense.ply?inline=1" in reponse.text


def test_visionneuse_fichier_inconnu_retombe_sur_le_premier(client_connecte, projet_reconstruit):
    _, job_id = projet_reconstruit
    reponse = client_connecte.get(f"/jobs/{job_id}/visionneuse", params={"fichier": "/etc/passwd"})
    assert reponse.status_code == 200
    assert "/etc/passwd" not in reponse.text


def test_page_sante(client_connecte, chaine_factice):
    reponse = client_connecte.get("/health")
    assert reponse.status_code == 200
    assert "Chaine complete detectee" in reponse.text
    assert "File d'attente" in reponse.text


def test_page_sante_sans_chaine(client_connecte):
    reponse = client_connecte.get("/health")
    assert "Aucune chaine installee" in reponse.text


def test_page_404(client_connecte):
    reponse = client_connecte.get("/projects/99999")
    assert reponse.status_code == 404


def test_profils_indisponibles_signales_sur_la_page_projet(client_connecte, photo_jpeg):
    """Sans chaine installee, la page doit le dire au lieu d'offrir un bouton mort."""
    reponse = client_connecte.post(
        "/projects", data={"name": "Vide", "description": "", "preset": "sparse"},
        follow_redirects=False,
    )
    project_id = int(reponse.headers["location"].rsplit("/", 1)[1])

    page = client_connecte.get(f"/projects/{project_id}")
    assert "Aucune chaine de reconstruction n'est installee" in page.text
    assert "indisponible" in page.text


@pytest.fixture
def machine_windows(monkeypatch):
    """Force les sondes a renvoyer ce que produirait une machine Windows.

    Impossible de tester sur le systeme cible depuis ici : on verifie au moins
    que les gabarits traitent la forme des donnees qu'il renvoie — pas de
    charge moyenne, un fichier d'echange plutot qu'un swap.
    """
    from app import system

    instantane = {
        "memoire": {
            "total": 17_000_000_000, "available": 9_000_000_000, "used": 8_000_000_000,
            "swap_total": 2_000_000_000, "swap_used": 100_000_000,
            "percent": 47.0, "complet": True,
        },
        "disque": {"total": 500_000_000_000, "free": 200_000_000_000,
                   "used": 300_000_000_000, "percent": 60.0},
        "charge": (0.0, 0.0, 0.0),   # getloadavg n'existe pas sous Windows
        "temperature": 0.0,
        "cpus": 8,
        "systeme": "nt",
    }
    monkeypatch.setattr(system, "snapshot", lambda: instantane)
    return instantane


def test_pages_sous_windows(client_connecte, machine_windows):
    accueil = client_connecte.get("/")
    assert accueil.status_code == 200
    # Sans charge moyenne, on montre le nombre de coeurs au lieu d'un « 0.00 ».
    assert "Coeurs" in accueil.text
    assert "Charge (" not in accueil.text

    sante = client_connecte.get("/health")
    assert sante.status_code == 200
    assert "Memoire virtuelle" in sante.text
    assert "Swap total" not in sante.text
    # L'avertissement sur dphys-swapfile ne concerne que Linux.
    assert "dphys-swapfile" not in sante.text


def test_memoire_incomplete_affichee_comme_inconnue(client_connecte, monkeypatch):
    """Un systeme dont on ne lit que le total ne doit pas afficher « 0 octet libre »."""
    from app import system

    monkeypatch.setattr(system, "snapshot", lambda: {
        "memoire": {"total": 8_000_000_000, "available": 0, "used": 0,
                    "swap_total": 0, "swap_used": 0, "percent": 0.0, "complet": False},
        "disque": {"total": 1, "free": 1, "used": 0, "percent": 0.0},
        "charge": (0.0, 0.0, 0.0), "temperature": 0.0, "cpus": 4, "systeme": "posix",
    })

    accueil = client_connecte.get("/")
    assert "RAM totale" in accueil.text
    assert "RAM libre" not in accueil.text

    sante = client_connecte.get("/health")
    assert "—" in sante.text
