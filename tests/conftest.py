"""Environnement de test : tout est redirige vers un repertoire temporaire.

Les variables doivent etre posees avant le premier import de app.config, qui
fige la configuration dans un singleton au chargement du module.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

_DATA_DIR = Path(tempfile.mkdtemp(prefix="photogram-tests-"))

os.environ["PHOTOGRAM_DATA_DIR"] = str(_DATA_DIR)
os.environ["PHOTOGRAM_PASSWORD"] = "motdepasse-de-test"
os.environ["PHOTOGRAM_SECRET_KEY"] = "a" * 64
os.environ["PHOTOGRAM_MAX_PHOTOS"] = "40"
os.environ["PHOTOGRAM_WORK_MAX_DIM"] = "800"
os.environ["PHOTOGRAM_THREADS"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def base_propre():
    """Chaque test part d'une base vide."""
    settings.ensure_dirs()
    db.init_db()
    with db.cursor() as conn:
        for table in ("artifacts", "job_steps", "jobs", "photos", "projects"):
            conn.execute(f"DELETE FROM {table}")
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def client_connecte(client):
    reponse = client.post(
        "/login",
        data={"password": "motdepasse-de-test", "next": "/"},
        follow_redirects=False,
    )
    assert reponse.status_code == 303
    return client


@pytest.fixture
def photo_jpeg():
    """Fabrique une image JPEG texturee, avec EXIF, en memoire."""
    import io

    from PIL import Image

    def fabriquer(largeur=1200, hauteur=900, focale=24, graine=0):
        image = Image.new("RGB", (largeur, hauteur), (40, 60, 90))
        pixels = image.load()
        # Un motif contraste : une image unie donnerait une nettete nulle et
        # fausserait les tests de tri qualitatif.
        for x in range(0, largeur, 3):
            for y in range(0, hauteur, 3):
                if (x + y + graine) % 7 < 3:
                    pixels[x, y] = (230, 220, 40)
        exif = image.getexif()
        exif[271] = "TestCam"
        exif[272] = "TestCam Model X"
        if focale:
            exif.get_ifd(0x8769)[37386] = (focale, 1)
        tampon = io.BytesIO()
        image.save(tampon, "JPEG", exif=exif.tobytes(), quality=88)
        return tampon.getvalue()

    return fabriquer


@pytest.fixture
def data_dir():
    return _DATA_DIR


# --- Binaires factices ----------------------------------------------------

STUB = r'''#!/usr/bin/env python3
"""Binaire factice : ecrit les fichiers qu'attend l'etape suivante."""
import os, pathlib, sys

nom = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]

def opt(drapeau, defaut=None):
    return args[args.index(drapeau) + 1] if drapeau in args else defaut

print("stub %s : %s" % (nom, " ".join(args)))

if "ECHOUE" in nom:
    sys.exit(3)

travail = pathlib.Path(opt("-w", "."))

if nom == "colmap":
    sous_commande = args[0]
    if sous_commande == "feature_extractor":
        if os.environ.get("STUB_SIFT_GPU_CASSE") == "1" and opt("--SiftExtraction.use_gpu") == "1":
            sys.stderr.write("ERROR: SiftGPU not fully supported on this system.\n")
            sys.exit(1)
        base = pathlib.Path(opt("--database_path"))
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text("base")
    elif sous_commande == "mapper":
        sortie = pathlib.Path(opt("--output_path"))
        nb = int(os.environ.get("STUB_COLMAP_MODELES", "1"))
        for index in range(nb):
            modele = sortie / str(index)
            modele.mkdir(parents=True, exist_ok=True)
            (modele / "images.bin").write_text("i" * (100 * (index + 1)))
    elif sous_commande == "image_undistorter":
        sortie = pathlib.Path(opt("--output_path"))
        for sous in ("images", "sparse", "stereo"):
            (sortie / sous).mkdir(parents=True, exist_ok=True)
    elif sous_commande == "patch_match_stereo":
        # Emule la version sans CUDA, qui refuse la densification avec un
        # message explicite : c'est sur lui que porte la detection.
        if os.environ.get("STUB_COLMAP_CUDA", "1") == "0":
            sys.stderr.write(
                "ERROR: Dense stereo reconstruction requires CUDA, which is not "
                "available on your system.\n"
            )
            sys.exit(1)
        travail = pathlib.Path(opt("--workspace_path"))
        cartes = travail / "stereo" / "depth_maps"
        cartes.mkdir(parents=True, exist_ok=True)
        (cartes / "vue.geometric.bin").write_text("profondeur")
    elif sous_commande == "stereo_fusion":
        cible = pathlib.Path(opt("--output_path"))
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_text(
            "ply\nformat ascii 1.0\nelement vertex 1\n"
            "property float x\nproperty float y\nproperty float z\n"
            "end_header\n0 0 0\n"
        )
    elif sous_commande == "poisson_mesher":
        pathlib.Path(opt("--output_path")).write_text("ply maillage\n")
    elif sous_commande == "model_converter":
        cible = pathlib.Path(opt("--output_path"))
        if opt("--output_type") == "PLY":
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_text(
                "ply\nformat ascii 1.0\nelement vertex 1\n"
                "property float x\nproperty float y\nproperty float z\n"
                "end_header\n0 0 0\n"
            )
        else:
            cible.mkdir(parents=True, exist_ok=True)
            (cible / "cameras.txt").write_text("# cameras\n")
    sys.exit(0)

if nom.endswith("SfMInit_ImageListing"):
    sortie = pathlib.Path(opt("-o"))
    sortie.mkdir(parents=True, exist_ok=True)
    (sortie / "sfm_data.json").write_text('{"sfm_data_version": "0.3"}')
elif nom.endswith("ComputeFeatures"):
    pass
elif nom.endswith("PairGenerator") or nom.endswith("GeometricFilter"):
    pathlib.Path(opt("-o")).write_text("paires")
elif nom.endswith("ComputeMatches"):
    cible = pathlib.Path(opt("-o"))
    if cible.suffix:
        cible.write_text("correspondances")
    else:
        cible.mkdir(parents=True, exist_ok=True)
        (cible / "matches.f.bin").write_text("correspondances")
elif nom.endswith("IncrementalSfM") or nom.endswith("main_SfM"):
    sortie = pathlib.Path(opt("--output_dir") or opt("-o"))
    sortie.mkdir(parents=True, exist_ok=True)
    (sortie / "sfm_data.bin").write_text("reconstruction" * 100)
elif nom.endswith("ComputeSfM_DataColor"):
    cible = pathlib.Path(opt("-o"))
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text(
        "ply\nformat ascii 1.0\nelement vertex 1\n"
        "property float x\nproperty float y\nproperty float z\n"
        "end_header\n0 0 0\n"
    )
elif nom.endswith("openMVG2openMVS"):
    cible = pathlib.Path(opt("-o"))
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text("scene")
    pathlib.Path(opt("-d")).mkdir(parents=True, exist_ok=True)
elif nom == "DensifyPointCloud":
    (travail / "dense.mvs").write_text("dense")
    (travail / "dense.ply").write_text("ply dense")
elif nom == "ReconstructMesh":
    (travail / "mesh.mvs").write_text("maillage")
    (travail / "mesh.ply").write_text("ply maillage")
elif nom == "TextureMesh":
    (travail / "texture.obj").write_text("o maillage\n")
    (travail / "texture.mtl").write_text("newmtl m\n")
    (travail / "texture.png").write_bytes(b"\x89PNG")
'''

TOUS_LES_BINAIRES = [
    "openMVG_main_SfMInit_ImageListing",
    "openMVG_main_ComputeFeatures",
    "openMVG_main_PairGenerator",
    "openMVG_main_ComputeMatches",
    "openMVG_main_GeometricFilter",
    "openMVG_main_SfM",
    "openMVG_main_ComputeSfM_DataColor",
    "openMVG_main_openMVG2openMVS",
    "DensifyPointCloud",
    "ReconstructMesh",
    "RefineMesh",
    "TextureMesh",
]


def activer_stubs(dossier: Path, monkeypatch) -> None:
    """Fait pointer la detection vers le dossier de stubs.

    Le PATH est prefixe, jamais remplace : les stubs doivent primer sur une
    eventuelle installation reelle, mais leur shebang a besoin du PATH systeme
    pour trouver l'interpreteur Python.
    """
    monkeypatch.setattr(settings, "openmvg_bin", str(dossier))
    monkeypatch.setattr(settings, "openmvs_bin", str(dossier))
    monkeypatch.setenv("PATH", str(dossier) + os.pathsep + os.environ.get("PATH", ""))


def installer_stubs(dossier: Path, noms) -> Path:
    dossier.mkdir(parents=True, exist_ok=True)
    for nom in noms:
        chemin = dossier / nom
        chemin.write_text(STUB)
        chemin.chmod(chemin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return dossier


@pytest.fixture
def chaine_factice(tmp_path, monkeypatch):
    """Chaine complete OpenMVG 2.x + OpenMVS, en version factice."""
    dossier = installer_stubs(tmp_path / "bin", TOUS_LES_BINAIRES)
    activer_stubs(dossier, monkeypatch)
    return dossier
