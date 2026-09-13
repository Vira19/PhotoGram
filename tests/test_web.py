"""Parcours web : authentification, projets, photos, mise en file."""

from __future__ import annotations

from app import db
from app.config import settings


def creer_projet(client, nom="Buste"):
    reponse = client.post(
        "/projects", data={"name": nom, "description": "", "preset": "sparse"},
        follow_redirects=False,
    )
    assert reponse.status_code == 303
    return int(reponse.headers["location"].rsplit("/", 1)[1])


def televerser(client, project_id, photo_jpeg, nombre=6):
    fichiers = [
        ("files", (f"IMG_{i:04d}.jpg", photo_jpeg(graine=i), "image/jpeg"))
        for i in range(nombre)
    ]
    reponse = client.post(f"/projects/{project_id}/photos", files=fichiers, follow_redirects=False)
    assert reponse.status_code == 303
    return reponse


# --- Authentification -----------------------------------------------------


def test_page_protegee_redirige_vers_login(client):
    reponse = client.get("/", follow_redirects=False)
    assert reponse.status_code == 303
    assert reponse.headers["location"].startswith("/login")


def test_mauvais_mot_de_passe_refuse(client):
    reponse = client.post("/login", data={"password": "faux", "next": "/"}, follow_redirects=False)
    assert reponse.status_code == 401
    assert "incorrect" in reponse.text.lower()


def test_connexion_puis_acces(client_connecte):
    reponse = client_connecte.get("/")
    assert reponse.status_code == 200
    assert "Projets" in reponse.text


def test_redirection_next_externe_ignoree(client):
    """Une cible externe ne doit jamais servir de redirection apres connexion."""
    reponse = client.post(
        "/login",
        data={"password": "motdepasse-de-test", "next": "https://exemple.invalide/vol"},
        follow_redirects=False,
    )
    assert reponse.status_code == 303
    assert reponse.headers["location"] == "/"


def test_health_accessible_sans_session(client):
    reponse = client.get("/health", headers={"accept": "application/json"})
    assert reponse.status_code == 200
    assert "pipeline" in reponse.json()


# --- Projets et photos ----------------------------------------------------


def test_creation_projet(client_connecte):
    project_id = creer_projet(client_connecte)
    assert settings.photos_dir(project_id).is_dir()
    assert client_connecte.get(f"/projects/{project_id}").status_code == 200


def test_upload_extrait_les_metadonnees(client_connecte, photo_jpeg):
    project_id = creer_projet(client_connecte)
    televerser(client_connecte, project_id, photo_jpeg, nombre=3)

    photos = db.fetch_all("SELECT * FROM photos WHERE project_id = ?", (project_id,))
    assert len(photos) == 3
    for photo in photos:
        assert photo["width"] == 1200 and photo["height"] == 900
        assert photo["camera"] == "TestCam Model X"
        assert photo["focal_mm"] == 24.0
        assert photo["sharpness"] > 0
        assert (settings.thumbs_dir(project_id) / photo["filename"]).is_file()
        assert (settings.work_dir(project_id) / photo["filename"]).is_file()


def test_copie_de_travail_est_reduite(client_connecte, photo_jpeg):
    from PIL import Image

    project_id = creer_projet(client_connecte)
    televerser(client_connecte, project_id, photo_jpeg, nombre=1)
    photo = db.fetch_all("SELECT * FROM photos")[0]

    with Image.open(settings.work_dir(project_id) / photo["filename"]) as image:
        assert max(image.size) == settings.work_max_dim


def test_fichier_non_image_rejete(client_connecte):
    project_id = creer_projet(client_connecte)
    reponse = client_connecte.post(
        f"/projects/{project_id}/photos",
        files=[("files", ("notes.txt", b"pas une image", "text/plain"))],
        follow_redirects=True,
    )
    assert reponse.status_code == 200
    assert db.fetch_all("SELECT * FROM photos") == []


def test_nom_de_fichier_hostile_neutralise(client_connecte, photo_jpeg):
    project_id = creer_projet(client_connecte)
    client_connecte.post(
        f"/projects/{project_id}/photos",
        files=[("files", ("../../../etc/passwd.jpg", photo_jpeg(), "image/jpeg"))],
        follow_redirects=True,
    )
    photo = db.fetch_all("SELECT * FROM photos")[0]
    assert "/" not in photo["filename"] and ".." not in photo["filename"]
    assert (settings.photos_dir(project_id) / photo["filename"]).is_file()


def test_bascule_inclusion(client_connecte, photo_jpeg):
    project_id = creer_projet(client_connecte)
    televerser(client_connecte, project_id, photo_jpeg, nombre=1)
    photo_id = db.fetch_all("SELECT id FROM photos")[0]["id"]

    reponse = client_connecte.post(f"/photos/{photo_id}/toggle")
    assert reponse.status_code == 200
    assert "Retenir" in reponse.text
    assert db.fetch_one("SELECT included FROM photos WHERE id = ?", (photo_id,))["included"] == 0

    client_connecte.post(f"/photos/{photo_id}/toggle")
    assert db.fetch_one("SELECT included FROM photos WHERE id = ?", (photo_id,))["included"] == 1


def test_suppression_projet_efface_les_fichiers(client_connecte, photo_jpeg):
    project_id = creer_projet(client_connecte)
    televerser(client_connecte, project_id, photo_jpeg, nombre=2)

    client_connecte.post(f"/projects/{project_id}/delete", follow_redirects=False)
    assert db.fetch_all("SELECT * FROM photos") == []
    assert not settings.project_dir(project_id).exists()


# --- Mise en file ---------------------------------------------------------


def test_refus_si_trop_peu_de_photos(client_connecte, photo_jpeg):
    project_id = creer_projet(client_connecte)
    televerser(client_connecte, project_id, photo_jpeg, nombre=3)

    reponse = client_connecte.post(f"/projects/{project_id}/jobs", data={"preset": "sparse"})
    assert reponse.status_code == 400
    assert "5 photos" in reponse.json()["detail"]


def test_mise_en_file(client_connecte, photo_jpeg, chaine_factice):
    project_id = creer_projet(client_connecte)
    televerser(client_connecte, project_id, photo_jpeg, nombre=6)

    reponse = client_connecte.post(
        f"/projects/{project_id}/jobs", data={"preset": "sparse"}, follow_redirects=False
    )
    assert reponse.status_code == 303
    job = db.fetch_all("SELECT * FROM jobs")[0]
    assert job["status"] == "queued" and job["photo_count"] == 6


def test_photos_ecartees_non_comptees(client_connecte, photo_jpeg, chaine_factice):
    project_id = creer_projet(client_connecte)
    televerser(client_connecte, project_id, photo_jpeg, nombre=8)
    for photo in db.fetch_all("SELECT id FROM photos LIMIT 2"):
        client_connecte.post(f"/photos/{photo['id']}/toggle")

    client_connecte.post(f"/projects/{project_id}/jobs", data={"preset": "sparse"}, follow_redirects=False)
    assert db.fetch_all("SELECT * FROM jobs")[0]["photo_count"] == 6


def test_annulation_avant_demarrage(client_connecte, photo_jpeg, chaine_factice):
    project_id = creer_projet(client_connecte)
    televerser(client_connecte, project_id, photo_jpeg, nombre=6)
    client_connecte.post(f"/projects/{project_id}/jobs", data={"preset": "sparse"}, follow_redirects=False)
    job_id = db.fetch_all("SELECT id FROM jobs")[0]["id"]

    client_connecte.post(f"/jobs/{job_id}/cancel", follow_redirects=False)
    assert db.fetch_one("SELECT status FROM jobs WHERE id = ?", (job_id,))["status"] == "cancelled"


def test_telechargement_hors_dossier_refuse(client_connecte, photo_jpeg, chaine_factice):
    """Un nom de fichier forge ne doit jamais sortir du dossier de resultats."""
    project_id = creer_projet(client_connecte)
    televerser(client_connecte, project_id, photo_jpeg, nombre=6)
    client_connecte.post(f"/projects/{project_id}/jobs", data={"preset": "sparse"}, follow_redirects=False)
    job_id = db.fetch_all("SELECT id FROM jobs")[0]["id"]

    reponse = client_connecte.get(f"/jobs/{job_id}/fichiers/..%2F..%2F..%2Fphotogram.db")
    assert reponse.status_code == 404


def test_profil_indisponible_refuse(client_connecte, photo_jpeg):
    """Sans chaine installee, la mise en file est refusee avec un motif clair."""
    project_id = creer_projet(client_connecte)
    televerser(client_connecte, project_id, photo_jpeg, nombre=6)

    reponse = client_connecte.post(f"/projects/{project_id}/jobs", data={"preset": "sparse"})
    assert reponse.status_code == 400
    assert "Binaires manquants" in reponse.json()["detail"]
