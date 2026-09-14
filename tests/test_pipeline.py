"""Pipeline et worker, joues avec des binaires factices.

Installer OpenMVG et OpenMVS pour tester l'orchestration serait disproportionne :
on remplace les binaires par des scripts qui produisent les memes fichiers, ce
qui verifie l'enchainement des etapes, la detection de version, la collecte des
resultats et la gestion des echecs.

Les stubs et les fixtures qui les activent vivent dans conftest.py, car les
tests web en ont besoin eux aussi pour mettre un job en file.
"""

from __future__ import annotations

import pytest
from conftest import STUB, TOUS_LES_BINAIRES, activer_stubs, installer_stubs

from app import db, worker
from app.config import settings
from app.pipeline.binaries import detect_toolchain
from app.pipeline.plan import PlanContext, build_plan
from app.pipeline.presets import get_preset


def preparer_projet(client, photo_jpeg, nombre=6, preset="sparse") -> int:
    reponse = client.post(
        "/projects", data={"name": "Sujet", "description": "", "preset": preset},
        follow_redirects=False,
    )
    project_id = int(reponse.headers["location"].rsplit("/", 1)[1])
    fichiers = [
        ("files", (f"IMG_{i:04d}.jpg", photo_jpeg(graine=i), "image/jpeg"))
        for i in range(nombre)
    ]
    client.post(f"/projects/{project_id}/photos", files=fichiers, follow_redirects=False)
    return project_id
def mettre_en_file(project_id: int, preset: str) -> int:
    retenues = db.fetch_one(
        "SELECT COUNT(*) AS n FROM photos WHERE project_id = ? AND included = 1", (project_id,)
    )["n"]
    return db.execute(
        "INSERT INTO jobs (project_id, status, preset, photo_count, created_at) "
        "VALUES (?, 'queued', ?, ?, ?)",
        (project_id, preset, retenues, db.now()),
    )


# --- Detection de la chaine ----------------------------------------------


def test_detection_openmvg_moderne(chaine_factice):
    tools = detect_toolchain()
    assert tools.modern_openmvg is True
    assert tools.has_openmvg and tools.has_openmvs
    assert tools.missing() == []


def test_detection_openmvg_ancien(tmp_path, monkeypatch):
    """Sans PairGenerator ni main_SfM, on doit basculer sur le flux 1.x."""
    anciens = [
        "openMVG_main_SfMInit_ImageListing", "openMVG_main_ComputeFeatures",
        "openMVG_main_ComputeMatches", "openMVG_main_IncrementalSfM",
        "openMVG_main_ComputeSfM_DataColor", "openMVG_main_openMVG2openMVS",
        "DensifyPointCloud", "ReconstructMesh", "TextureMesh",
    ]
    dossier = installer_stubs(tmp_path / "bin-ancien", anciens)
    activer_stubs(dossier, monkeypatch)

    tools = detect_toolchain()
    assert tools.modern_openmvg is False
    assert tools.missing() == []

    ctx = PlanContext(1, 1, tmp_path / "job", get_preset("sparse"), tools, 1)
    noms = [etape.name for etape in build_plan(ctx)]
    assert "Generation des paires d'images" not in noms
    assert "Mise en correspondance et filtrage" in noms


def test_plan_sparse_ignore_openmvs(chaine_factice, tmp_path):
    ctx = PlanContext(1, 1, tmp_path / "job", get_preset("sparse"), detect_toolchain(), 1)
    noms = [etape.name for etape in build_plan(ctx)]
    assert "Densification du nuage" not in noms
    assert noms[-1] == "Collecte des resultats"


def test_plan_complet_contient_openmvs(chaine_factice, tmp_path):
    ctx = PlanContext(1, 1, tmp_path / "job", get_preset("rpi"), detect_toolchain(), 1)
    noms = [etape.name for etape in build_plan(ctx)]
    assert "Densification du nuage" in noms
    assert "Reconstruction du maillage" in noms
    assert "Raffinement du maillage" not in noms  # desactive sur ce profil


# --- Execution complete ---------------------------------------------------


def test_job_sparse_aboutit(client_connecte, photo_jpeg, chaine_factice):
    project_id = preparer_projet(client_connecte, photo_jpeg)
    job_id = mettre_en_file(project_id, "sparse")

    job = worker.claim_job()
    assert job["id"] == job_id
    worker.process_job(job)

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "done", resultat["error"]

    etapes = db.fetch_all("SELECT * FROM job_steps WHERE job_id = ? ORDER BY position", (job_id,))
    assert etapes and all(e["status"] == "done" for e in etapes)

    fichiers = {a["filename"] for a in db.fetch_all("SELECT * FROM artifacts WHERE job_id = ?", (job_id,))}
    assert "nuage_epars.ply" in fichiers

    journal = (settings.job_dir(project_id, job_id) / "job.log").read_text()
    assert "Reconstruction terminee avec succes" in journal


def test_job_complet_produit_un_maillage(client_connecte, photo_jpeg, chaine_factice):
    project_id = preparer_projet(client_connecte, photo_jpeg, preset="rpi")
    job_id = mettre_en_file(project_id, "rpi")

    worker.process_job(worker.claim_job())

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "done", resultat["error"]

    fichiers = {a["filename"] for a in db.fetch_all("SELECT * FROM artifacts WHERE job_id = ?", (job_id,))}
    assert {"dense.ply", "mesh.ply", "texture.obj", "texture.mtl", "texture.png"} <= fichiers


def test_intermediaires_nettoyes(client_connecte, photo_jpeg, chaine_factice):
    """Les dossiers de travail doivent disparaitre : une carte SD se remplit vite."""
    project_id = preparer_projet(client_connecte, photo_jpeg, preset="rpi")
    job_id = mettre_en_file(project_id, "rpi")
    worker.process_job(worker.claim_job())

    job_dir = settings.job_dir(project_id, job_id)
    assert not (job_dir / "mvs").exists()
    assert not (job_dir / "mvg").exists()
    assert not (job_dir / "images").exists()
    assert (job_dir / "out").is_dir()


def test_echec_binaire_marque_le_job(client_connecte, photo_jpeg, tmp_path, monkeypatch):
    """Un binaire qui sort en erreur doit stopper le job et etre trace."""
    noms = list(TOUS_LES_BINAIRES)
    dossier = installer_stubs(tmp_path / "bin-ko", noms)
    # Le detecteur de points caracteristiques echoue systematiquement.
    (dossier / "openMVG_main_ComputeFeatures").write_text(
        STUB.replace('nom = pathlib.Path(sys.argv[0]).name', 'nom = "ECHOUE"')
    )
    (dossier / "openMVG_main_ComputeFeatures").chmod(0o755)
    activer_stubs(dossier, monkeypatch)

    project_id = preparer_projet(client_connecte, photo_jpeg)
    job_id = mettre_en_file(project_id, "sparse")
    worker.process_job(worker.claim_job())

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "failed"
    assert "points caracteristiques" in resultat["error"]

    etape = db.fetch_one(
        "SELECT * FROM job_steps WHERE job_id = ? AND name LIKE '%caracteristiques%'", (job_id,)
    )
    assert etape["status"] == "failed"


def test_sfm_sans_resultat_est_detecte(client_connecte, photo_jpeg, tmp_path, monkeypatch):
    """Un SfM qui sort en code 0 sans rien produire ne doit pas passer inapercu."""
    dossier = installer_stubs(tmp_path / "bin-vide", TOUS_LES_BINAIRES)
    (dossier / "openMVG_main_SfM").write_text("#!/bin/sh\nexit 0\n")
    (dossier / "openMVG_main_SfM").chmod(0o755)
    activer_stubs(dossier, monkeypatch)

    project_id = preparer_projet(client_connecte, photo_jpeg)
    job_id = mettre_en_file(project_id, "sparse")
    worker.process_job(worker.claim_job())

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "failed"
    assert "recouvrement" in resultat["error"]


def test_binaires_absents_signales(client_connecte, photo_jpeg, tmp_path, monkeypatch):
    dossier = installer_stubs(tmp_path / "bin-vide2", [])
    activer_stubs(dossier, monkeypatch)

    project_id = preparer_projet(client_connecte, photo_jpeg)
    job_id = mettre_en_file(project_id, "sparse")
    worker.process_job(worker.claim_job())

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "failed"
    assert "Binaires manquants" in resultat["error"]


def test_job_orphelin_remis_en_file(client_connecte, photo_jpeg):
    """Apres une coupure, un job 'running' doit repartir au lieu de rester fige."""
    project_id = preparer_projet(client_connecte, photo_jpeg)
    job_id = mettre_en_file(project_id, "sparse")
    db.execute("UPDATE jobs SET status = 'running', step_index = 3 WHERE id = ?", (job_id,))

    worker.requeue_orphans()

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "queued" and resultat["step_index"] == 0


def test_un_seul_worker_reclame_un_job(client_connecte, photo_jpeg):
    project_id = preparer_projet(client_connecte, photo_jpeg)
    mettre_en_file(project_id, "sparse")

    premier = worker.claim_job()
    second = worker.claim_job()
    assert premier is not None
    assert second is None, "un job deja reclame ne doit pas l'etre une seconde fois"


def test_focale_de_repli_seulement_sans_exif(client_connecte, photo_jpeg):
    project_id = preparer_projet(client_connecte, photo_jpeg)
    assert worker.fallback_focal_px(project_id) is None  # les photos portent une focale

    db.execute("UPDATE photos SET focal_mm = NULL WHERE project_id = ?", (project_id,))
    repli = worker.fallback_focal_px(project_id)
    assert repli is not None
    # 1,2 x le cote long de la copie de travail (800 px ici).
    assert repli == pytest.approx(1.2 * settings.work_max_dim, rel=0.02)


# --- Backend COLMAP -------------------------------------------------------


@pytest.fixture
def chaine_colmap(tmp_path, monkeypatch):
    """COLMAP seul, compile sans CUDA : ni OpenMVG ni OpenMVS."""
    monkeypatch.setenv("STUB_COLMAP_CUDA", "0")
    dossier = installer_stubs(tmp_path / "bin-colmap", ["colmap"])
    activer_stubs(dossier, monkeypatch)
    return dossier


def test_colmap_choisi_quand_openmvg_absent(chaine_colmap):
    tools = detect_toolchain()
    assert tools.has_colmap and not tools.has_openmvg
    assert tools.backends() == ["colmap"]
    assert tools.choose_backend("auto", sparse_only=True) == "colmap"
    assert tools.missing_for("colmap", sparse_only=True) == []


def test_openmvg_prefere_quand_les_deux_sont_la(tmp_path, monkeypatch):
    dossier = installer_stubs(tmp_path / "bin-deux", TOUS_LES_BINAIRES + ["colmap"])
    activer_stubs(dossier, monkeypatch)

    tools = detect_toolchain()
    assert tools.backends() == ["openmvg", "colmap"]
    assert tools.choose_backend("auto", sparse_only=True) == "openmvg"
    assert tools.choose_backend("colmap", sparse_only=True) == "colmap"


def test_openmvs_non_requis_pour_un_profil_epars(tmp_path, monkeypatch):
    """Un RPi n'ayant que le SfM d'OpenMVG doit pouvoir sortir un nuage epars."""
    sans_mvs = [n for n in TOUS_LES_BINAIRES if n[0].islower() or n.startswith("openMVG")]
    sans_mvs = [n for n in sans_mvs if n != "openMVG_main_openMVG2openMVS"]
    dossier = installer_stubs(tmp_path / "bin-sans-mvs", sans_mvs)
    activer_stubs(dossier, monkeypatch)

    tools = detect_toolchain()
    assert tools.missing_for("openmvg", sparse_only=True) == []
    assert tools.missing_for("openmvg", sparse_only=False) != []


def test_job_colmap_aboutit(client_connecte, photo_jpeg, chaine_colmap):
    project_id = preparer_projet(client_connecte, photo_jpeg)
    job_id = mettre_en_file(project_id, "sparse")

    worker.process_job(worker.claim_job())

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "done", resultat["error"]
    fichiers = {a["filename"] for a in db.fetch_all("SELECT * FROM artifacts WHERE job_id = ?", (job_id,))}
    assert "nuage_epars.ply" in fichiers

    journal = (settings.job_dir(project_id, job_id) / "job.log").read_text()
    assert "Chaine utilisee : colmap" in journal


def test_colmap_refuse_un_profil_maillage(client_connecte, photo_jpeg, chaine_colmap):
    """Sans OpenMVS, demander un maillage doit echouer tot et clairement."""
    project_id = preparer_projet(client_connecte, photo_jpeg, preset="rpi")
    job_id = mettre_en_file(project_id, "rpi")

    worker.process_job(worker.claim_job())

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "failed"
    assert "nuage epars" in resultat["error"]
    # L'echec doit survenir avant tout calcul : aucune etape ne doit avoir tourne.
    assert db.fetch_all("SELECT * FROM job_steps WHERE job_id = ?", (job_id,)) == []


def test_colmap_signale_une_serie_fragmentee(client_connecte, photo_jpeg, chaine_colmap, monkeypatch):
    monkeypatch.setenv("STUB_COLMAP_MODELES", "3")
    project_id = preparer_projet(client_connecte, photo_jpeg)
    job_id = mettre_en_file(project_id, "sparse")

    worker.process_job(worker.claim_job())

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "done", resultat["error"]
    journal = (settings.job_dir(project_id, job_id) / "job.log").read_text()
    assert "serie est fragmentee" in journal
    # Le plus gros modele (le dernier ecrit par le stub) doit etre retenu.
    assert "sparse/2" in journal


def test_colmap_sans_modele_echoue(client_connecte, photo_jpeg, chaine_colmap, monkeypatch):
    monkeypatch.setenv("STUB_COLMAP_MODELES", "0")
    project_id = preparer_projet(client_connecte, photo_jpeg)
    job_id = mettre_en_file(project_id, "sparse")

    worker.process_job(worker.claim_job())

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "failed"
    assert "recouvrement" in resultat["error"]


def test_orphelin_abandonne_apres_deux_tentatives(client_connecte, photo_jpeg):
    """Un job qui tue le worker a chaque passage ne doit pas bloquer la file."""
    project_id = preparer_projet(client_connecte, photo_jpeg)
    job_id = mettre_en_file(project_id, "sparse")
    db.execute(
        "UPDATE jobs SET status = 'running', attempts = ? WHERE id = ?",
        (worker.MAX_TENTATIVES, job_id),
    )

    worker.requeue_orphans()

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "failed"
    assert "memoire" in resultat["error"]


def test_compteur_de_tentatives_incremente(client_connecte, photo_jpeg):
    project_id = preparer_projet(client_connecte, photo_jpeg)
    job_id = mettre_en_file(project_id, "sparse")

    worker.claim_job()
    assert db.fetch_one("SELECT attempts FROM jobs WHERE id = ?", (job_id,))["attempts"] == 1


def test_isolation_processus_selon_le_systeme(monkeypatch):
    """Chaque systeme a son mecanisme de groupe de processus."""
    from app.pipeline import runner

    monkeypatch.setattr(runner, "WINDOWS", False)
    assert runner._isolation_processus() == {"start_new_session": True}

    monkeypatch.setattr(runner, "WINDOWS", True)
    options = runner._isolation_processus()
    assert "creationflags" in options
    assert "start_new_session" not in options


def test_surveillance_du_lanceur_sur_posix():
    """Sur POSIX, le worker se repere au PPID ; un PID errone signale l'absence."""
    import os

    assert worker._parent_vivant(os.getppid()) is True
    assert worker._parent_vivant(os.getppid() + 999_999) is False


def test_resume_annonce_colmap_plutot_que_les_absents(chaine_colmap):
    """Annoncer l'absence d'OpenMVG quand COLMAP suffit inquiete pour rien."""
    from app.pipeline import resume_chaine

    niveau, lignes = resume_chaine(detect_toolchain())
    texte = " ".join(lignes)
    assert niveau == "partiel"
    assert "COLMAP detecte" in texte
    assert "Aucune chaine" not in texte


def test_resume_chaine_complete(chaine_factice):
    from app.pipeline import resume_chaine

    niveau, lignes = resume_chaine(detect_toolchain())
    assert niveau == "complet"
    assert "OpenMVG" in " ".join(lignes) and "OpenMVS" in " ".join(lignes)


def test_resume_sans_rien(tmp_path, monkeypatch):
    from app.pipeline import resume_chaine

    activer_stubs(installer_stubs(tmp_path / "vide", []), monkeypatch)
    niveau, lignes = resume_chaine(detect_toolchain())
    assert niveau == "absent"
    # Le message doit orienter vers la solution la plus simple.
    assert "COLMAP" in " ".join(lignes)


def test_resume_openmvg_sans_openmvs(tmp_path, monkeypatch):
    """Un SfM seul reste utile : il ne faut pas le presenter comme inutilisable."""
    from app.pipeline import resume_chaine

    mvg = [n for n in TOUS_LES_BINAIRES if n.startswith("openMVG")]
    activer_stubs(installer_stubs(tmp_path / "mvg-seul", mvg), monkeypatch)

    niveau, lignes = resume_chaine(detect_toolchain())
    assert niveau == "partiel"
    assert "Nuage epars" in " ".join(lignes)


# --- COLMAP avec CUDA -----------------------------------------------------


@pytest.fixture
def chaine_colmap_cuda(tmp_path, monkeypatch):
    """COLMAP compile avec CUDA : patch_match_stereo ne refuse pas la tache."""
    monkeypatch.setenv("STUB_COLMAP_CUDA", "1")
    dossier = installer_stubs(tmp_path / "bin-cuda", ["colmap"])
    activer_stubs(dossier, monkeypatch)
    return dossier


def test_cuda_detecte_en_interrogeant_colmap(chaine_colmap_cuda):
    tools = detect_toolchain()
    assert tools.colmap_cuda is True
    assert tools.colmap_dense is True
    assert tools.missing_for("colmap", sparse_only=False) == []


def test_sans_cuda_le_maillage_est_signale_manquant(chaine_colmap):
    tools = detect_toolchain()
    assert tools.colmap_dense is False
    assert tools.missing_for("colmap", sparse_only=True) == []
    assert tools.missing_for("colmap", sparse_only=False) != []


def test_reglage_force_la_detection(chaine_colmap, monkeypatch):
    """La detection par bibliotheque voisine doit pouvoir etre contredite."""
    monkeypatch.setattr(settings, "colmap_cuda", "1")
    assert detect_toolchain().colmap_cuda is True

    monkeypatch.setattr(settings, "colmap_cuda", "0")
    assert detect_toolchain().colmap_cuda is False


def test_plan_dense_colmap(chaine_colmap_cuda, tmp_path):
    from app.pipeline.plan import PlanContext

    ctx = PlanContext(1, 1, tmp_path / "job", get_preset("balanced"), detect_toolchain(), 4)
    ctx.backend = "colmap"
    noms = [etape.name for etape in build_plan(ctx)]
    assert "Calcul des cartes de profondeur (GPU)" in noms
    assert "Fusion du nuage dense" in noms
    assert noms[-1] == "Collecte des resultats"


def test_profil_epars_ignore_les_etapes_denses(chaine_colmap_cuda, tmp_path):
    from app.pipeline.plan import PlanContext

    ctx = PlanContext(1, 1, tmp_path / "job", get_preset("sparse"), detect_toolchain(), 4)
    ctx.backend = "colmap"
    noms = [etape.name for etape in build_plan(ctx)]
    assert not any("profondeur" in n for n in noms)


def test_job_dense_colmap_aboutit(client_connecte, photo_jpeg, chaine_colmap_cuda):
    project_id = preparer_projet(client_connecte, photo_jpeg, preset="balanced")
    job_id = mettre_en_file(project_id, "balanced")

    worker.process_job(worker.claim_job())

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "done", resultat["error"]

    fichiers = {a["filename"] for a in db.fetch_all("SELECT * FROM artifacts WHERE job_id = ?", (job_id,))}
    assert {"nuage_epars.ply", "nuage_dense.ply", "maillage.ply"} <= fichiers

    journal = (settings.job_dir(project_id, job_id) / "job.log").read_text()
    assert "Chaine utilisee : colmap" in journal


def _valeur_option(argv, option: str) -> str:
    argv = [str(a) for a in argv]
    return argv[argv.index(option) + 1]


def test_sift_sur_gpu_seulement_avec_cuda(tmp_path, monkeypatch):
    """L'extraction SIFT doit exploiter le GPU quand il est la, pas sinon.

    Les deux chaines sont construites explicitement plutot que par deux
    fixtures concurrentes, dont l'ordre d'application decidrait du resultat.
    """
    from app.pipeline.colmap import _appariement_argv, _extraction_argv
    from app.pipeline.plan import PlanContext

    def contexte(avec_cuda: bool):
        monkeypatch.setenv("STUB_COLMAP_CUDA", "1" if avec_cuda else "0")
        dossier = installer_stubs(tmp_path / f"bin-{avec_cuda}", ["colmap"])
        activer_stubs(dossier, monkeypatch)
        return PlanContext(1, 1, tmp_path / "job", get_preset("sparse"), detect_toolchain(), 4)

    avec = contexte(True)
    assert avec.tools.colmap_dense is True
    assert _valeur_option(_extraction_argv(avec), "--SiftExtraction.use_gpu") == "1"
    assert _valeur_option(_appariement_argv(avec), "--SiftMatching.use_gpu") == "1"

    sans = contexte(False)
    assert sans.tools.colmap_dense is False
    assert _valeur_option(_extraction_argv(sans), "--SiftExtraction.use_gpu") == "0"
    assert _valeur_option(_appariement_argv(sans), "--SiftMatching.use_gpu") == "0"


def test_plafond_de_resolution_traduit_pour_colmap(tmp_path, monkeypatch):
    """COLMAP attend -1 pour « sans limite », la ou les profils ecrivent 0."""
    from app.pipeline.colmap import _stereo_argv
    from app.pipeline.plan import PlanContext

    monkeypatch.setenv("STUB_COLMAP_CUDA", "1")
    dossier = installer_stubs(tmp_path / "bin-res", ["colmap"])
    activer_stubs(dossier, monkeypatch)

    haute = PlanContext(1, 1, tmp_path / "j", get_preset("high"), detect_toolchain(), 4)
    assert get_preset("high").densify_max_resolution == 0
    assert _valeur_option(_stereo_argv(haute), "--PatchMatchStereo.max_image_size") == "-1"

    equilibre = PlanContext(1, 1, tmp_path / "j", get_preset("balanced"), detect_toolchain(), 4)
    assert _valeur_option(_stereo_argv(equilibre), "--PatchMatchStereo.max_image_size") == "1600"


def test_cuda_non_infirme_par_une_simple_mention(tmp_path, monkeypatch):
    """Une version AVEC CUDA mentionne « CUDA » sans que ce soit un refus.

    Chercher le seul mot « cuda » dans la sortie inverserait le diagnostic sur
    une version qui se contente d'annoncer les peripheriques detectes.
    """
    from app.pipeline.binaries import _sonder_cuda

    dossier = tmp_path / "bin-bavard"
    dossier.mkdir()
    faux = dossier / "colmap"
    faux.write_text(
        "#!/bin/sh\n"
        "echo 'Found 1 CUDA device'\n"
        "echo '  Device 0: NVIDIA GeForce RTX 4070'\n"
        "echo 'ERROR: workspace_path does not exist' >&2\n"
        "exit 1\n"
    )
    faux.chmod(0o755)

    assert _sonder_cuda(str(faux)) is True


def test_refus_explicite_detecte(tmp_path):
    from app.pipeline.binaries import _sonder_cuda

    dossier = tmp_path / "bin-refus"
    dossier.mkdir()
    faux = dossier / "colmap"
    faux.write_text(
        "#!/bin/sh\n"
        "echo 'ERROR: Dense stereo reconstruction requires CUDA, which is not "
        "available on your system.' >&2\n"
        "exit 1\n"
    )
    faux.chmod(0o755)

    assert _sonder_cuda(str(faux)) is False


def test_sonde_muette_reste_sans_avis(tmp_path):
    """Sans sortie exploitable, la sonde ne doit pas trancher au hasard."""
    from app.pipeline.binaries import _sonder_cuda

    dossier = tmp_path / "bin-muet"
    dossier.mkdir()
    faux = dossier / "colmap"
    faux.write_text("#!/bin/sh\nexit 0\n")
    faux.chmod(0o755)

    assert _sonder_cuda(str(faux)) is None


def test_sonde_mise_en_cache(tmp_path):
    """La detection est appelee a chaque affichage de page : sonder une fois suffit."""
    from app.pipeline.binaries import _CACHE_SONDE, _sonder_cuda

    dossier = tmp_path / "bin-cache"
    dossier.mkdir()
    compteur = dossier / "appels.txt"
    faux = dossier / "colmap"
    faux.write_text(f"#!/bin/sh\necho x >> {compteur}\necho 'requires CUDA' >&2\nexit 1\n")
    faux.chmod(0o755)

    _CACHE_SONDE.clear()
    assert _sonder_cuda(str(faux)) is False
    assert _sonder_cuda(str(faux)) is False
    assert compteur.read_text().count("x") == 1


def test_indices_sur_le_nom_de_dossier(tmp_path):
    """Faisceau d'indices, quand COLMAP n'a pas pu etre interroge."""
    from app.pipeline.binaries import _indices_cuda

    avec = tmp_path / "colmap-x64-windows-cuda" / "bin" / "colmap.exe"
    sans = tmp_path / "colmap-x64-windows-no-cuda" / "bin" / "colmap.exe"
    for chemin in (avec, sans):
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text("")

    assert _indices_cuda(str(avec)) is True
    # « no-cuda » contient « cuda » : l'ordre des tests compte.
    assert _indices_cuda(str(sans)) is False


def test_reglage_prime_sur_la_sonde(tmp_path, monkeypatch):
    """L'utilisateur doit pouvoir contredire une detection erronee."""
    from app.pipeline.binaries import _colmap_avec_cuda

    dossier = tmp_path / "bin-force"
    dossier.mkdir()
    faux = dossier / "colmap"
    faux.write_text("#!/bin/sh\necho 'requires CUDA' >&2\nexit 1\n")
    faux.chmod(0o755)

    assert _colmap_avec_cuda(str(faux)) is False
    monkeypatch.setattr(settings, "colmap_cuda", "1")
    assert _colmap_avec_cuda(str(faux)) is True


# --- Repli sur le processeur ---------------------------------------------


def test_repli_sur_processeur_quand_le_gpu_se_derobe(
    client_connecte, photo_jpeg, chaine_colmap_cuda, monkeypatch
):
    """Un echec d'acceleration graphique ne doit pas couter la reconstruction."""
    monkeypatch.setenv("STUB_SIFT_GPU_CASSE", "1")

    project_id = preparer_projet(client_connecte, photo_jpeg)
    job_id = mettre_en_file(project_id, "sparse")

    worker.process_job(worker.claim_job())

    resultat = db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert resultat["status"] == "done", resultat["error"]

    journal = (settings.job_dir(project_id, job_id) / "job.log").read_text()
    assert "Nouvel essai avec des options de repli" in journal
    assert "--SiftExtraction.use_gpu 0" in journal


def test_reglage_gpu_desactive_des_le_depart(tmp_path, monkeypatch):
    from app.pipeline.colmap import _extraction_argv
    from app.pipeline.plan import PlanContext

    monkeypatch.setenv("STUB_COLMAP_CUDA", "1")
    activer_stubs(installer_stubs(tmp_path / "bin-gpu", ["colmap"]), monkeypatch)
    ctx = PlanContext(1, 1, tmp_path / "j", get_preset("sparse"), detect_toolchain(), 4)

    assert _valeur_option(_extraction_argv(ctx), "--SiftExtraction.use_gpu") == "1"

    monkeypatch.setattr(settings, "colmap_gpu", "0")
    assert _valeur_option(_extraction_argv(ctx), "--SiftExtraction.use_gpu") == "0"


def test_pas_de_repli_si_deja_sur_processeur(tmp_path, monkeypatch):
    """Sans GPU au depart, il n'y a rien a retenter : l'echec est reel."""
    from app.pipeline.colmap import _extraction_argv, _repli_sans_gpu
    from app.pipeline.plan import PlanContext

    monkeypatch.setenv("STUB_COLMAP_CUDA", "1")
    activer_stubs(installer_stubs(tmp_path / "bin-nogpu", ["colmap"]), monkeypatch)
    monkeypatch.setattr(settings, "colmap_gpu", "0")

    ctx = PlanContext(1, 1, tmp_path / "j", get_preset("sparse"), detect_toolchain(), 4)
    repli = _repli_sans_gpu(_extraction_argv, "--SiftExtraction.use_gpu")
    assert repli(ctx, "error: siftgpu not fully supported") is None


def test_repli_ignore_une_erreur_sans_rapport(tmp_path, monkeypatch):
    """Une panne de disque ne doit pas etre confondue avec un souci de GPU."""
    from app.pipeline.colmap import _extraction_argv, _repli_sans_gpu
    from app.pipeline.plan import PlanContext

    monkeypatch.setenv("STUB_COLMAP_CUDA", "1")
    activer_stubs(installer_stubs(tmp_path / "bin-disque", ["colmap"]), monkeypatch)

    ctx = PlanContext(1, 1, tmp_path / "j", get_preset("sparse"), detect_toolchain(), 4)
    repli = _repli_sans_gpu(_extraction_argv, "--SiftExtraction.use_gpu")
    assert repli(ctx, "error: no space left on device") is None


# --- Messages d'echec -----------------------------------------------------


def test_message_d_echec_porte_la_cause():
    """« code de sortie 1 » seul n'apprend rien a l'utilisateur."""
    from app.pipeline.plan import Step
    from app.pipeline.runner import _message_echec

    message = _message_echec(
        Step("Detection des points caracteristiques"),
        1,
        ["Reading images...", "ERROR: SiftGPU not fully supported."],
    )
    assert "code de sortie 1" in message
    assert "SiftGPU not fully supported" in message
    # Et le conseil qui va avec.
    assert "PHOTOGRAM_COLMAP_GPU=0" in message


def test_message_d_echec_privilegie_les_lignes_d_erreur():
    from app.pipeline.plan import Step
    from app.pipeline.runner import _message_echec

    message = _message_echec(
        Step("Etape"), 2,
        ["Processing 1/50", "ERROR: out of memory", "Processing 2/50", "Done reading"],
    )
    assert "out of memory" in message
    assert "Processing 2/50" not in message
    assert "PHOTOGRAM_WORK_MAX_DIM" in message


def test_message_d_echec_sans_ligne_marquante():
    """Sans ligne d'erreur identifiable, les dernieres lignes restent utiles."""
    from app.pipeline.plan import Step
    from app.pipeline.runner import _message_echec

    message = _message_echec(Step("Etape"), 1, ["premiere", "derniere"])
    assert "derniere" in message
