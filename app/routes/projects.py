"""Projets et photos : creation, televersement, tri qualitatif."""

from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path
from typing import List

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .. import db, imaging, system
from ..config import settings
from ..pipeline import detect_toolchain, preset_availability
from ..pipeline.presets import DEFAULT_PRESET, PRESETS
from ..templating import templates

router = APIRouter()

ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
UPLOAD_CHUNK = 1024 * 1024


def safe_filename(name: str) -> str:
    """Nom de fichier ASCII, sans separateur de chemin.

    Les photos arrivent depuis des telephones et des appareils dont les noms
    de fichiers sont imprevisibles ; elles finissent en argument de commandes
    externes, donc on normalise agressivement.
    """
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return (stem or "photo") + (suffix or ".jpg")


def unique_filename(directory: Path, name: str) -> str:
    candidate = name
    stem, suffix = Path(name).stem, Path(name).suffix
    index = 1
    while (directory / candidate).exists():
        candidate = f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def get_project(project_id: int) -> dict:
    row = db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Projet introuvable")
    return dict(row)


def project_stats(project_id: int) -> dict:
    row = db.fetch_one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN included = 1 THEN 1 ELSE 0 END) AS retenues, "
        "SUM(bytes) AS octets, AVG(sharpness) AS nettete_moyenne "
        "FROM photos WHERE project_id = ?",
        (project_id,),
    )
    return {
        "total": row["total"] or 0,
        "retenues": row["retenues"] or 0,
        "octets": row["octets"] or 0,
        "nettete_moyenne": row["nettete_moyenne"],
    }


@router.get("/")
async def index(request: Request):
    projets = db.fetch_all(
        "SELECT p.*, "
        "(SELECT COUNT(*) FROM photos WHERE project_id = p.id) AS nb_photos, "
        "(SELECT COUNT(*) FROM jobs WHERE project_id = p.id) AS nb_jobs, "
        "(SELECT status FROM jobs WHERE project_id = p.id ORDER BY id DESC LIMIT 1) AS dernier_statut "
        "FROM projects p ORDER BY p.id DESC"
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "projets": projets,
            "systeme": system.snapshot(),
            "presets": PRESETS,
            "preset_defaut": DEFAULT_PRESET,
        },
    )


@router.post("/projects")
async def create_project(
    name: str = Form(...),
    description: str = Form(""),
    preset: str = Form(DEFAULT_PRESET),
):
    name = name.strip()[:120]
    if not name:
        raise HTTPException(status_code=400, detail="Le nom du projet est obligatoire.")
    if preset not in PRESETS:
        preset = DEFAULT_PRESET

    project_id = db.execute(
        "INSERT INTO projects (name, description, preset, created_at) VALUES (?, ?, ?, ?)",
        (name, description.strip()[:2000], preset, db.now()),
    )
    for directory in (
        settings.photos_dir(project_id),
        settings.thumbs_dir(project_id),
        settings.work_dir(project_id),
    ):
        directory.mkdir(parents=True, exist_ok=True)

    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}")
async def project_detail(request: Request, project_id: int):
    projet = get_project(project_id)
    photos = db.fetch_all(
        "SELECT * FROM photos WHERE project_id = ? ORDER BY filename", (project_id,)
    )
    jobs = db.fetch_all(
        "SELECT * FROM jobs WHERE project_id = ? ORDER BY id DESC LIMIT 20", (project_id,)
    )
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            # Le bilan du dernier televersement ne doit s'afficher qu'une fois.
            "flash": request.session.pop("flash", None),
            "projet": projet,
            "photos": photos,
            "jobs": jobs,
            "stats": project_stats(project_id),
            "presets": PRESETS,
            "dispo_presets": preset_availability(detect_toolchain(), settings.backend),
            "seuil_nettete": imaging.SHARPNESS_FLOOR,
            "max_photos": settings.max_photos,
            "max_upload_mb": settings.max_upload_mb,
            "work_max_dim": settings.work_max_dim,
        },
    )


@router.post("/projects/{project_id}/delete")
async def delete_project(project_id: int):
    get_project(project_id)
    db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    shutil.rmtree(settings.project_dir(project_id), ignore_errors=True)
    return RedirectResponse("/", status_code=303)


def _ingest(project_id: int, original_name: str, destination: Path, size: int) -> None:
    """Analyse une photo fraichement recue et l'enregistre en base.

    Appele dans un thread : Pillow et numpy liberent le GIL pendant le gros du
    travail, mais la decompression JPEG reste assez longue sur un RPi3 pour
    figer la boucle d'evenements si on l'executait en ligne.
    """
    meta = imaging.read_meta(destination)
    score = imaging.sharpness_score(destination)

    imaging.make_thumbnail(destination, settings.thumbs_dir(project_id) / destination.name)
    imaging.make_work_copy(
        destination,
        settings.work_dir(project_id) / destination.name,
        settings.work_max_dim,
    )

    db.execute(
        "INSERT INTO photos (project_id, filename, original_name, width, height, bytes, "
        "sharpness, focal_mm, camera, taken_at, included, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            project_id, destination.name, original_name[:255],
            meta["width"], meta["height"], size,
            score, meta["focal_mm"], meta["camera"], meta["taken_at"], db.now(),
        ),
    )


@router.post("/projects/{project_id}/photos")
async def upload_photos(
    request: Request,
    project_id: int,
    files: List[UploadFile] = File(...),
):
    get_project(project_id)
    photos_dir = settings.photos_dir(project_id)
    photos_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = settings.max_upload_mb * 1024 * 1024
    acceptees, rejetees = 0, []

    for upload in files:
        if not upload.filename:
            continue
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            rejetees.append(f"{upload.filename} : format non pris en charge")
            continue

        name = unique_filename(photos_dir, safe_filename(upload.filename))
        destination = photos_dir / name

        # Ecriture en flux : une photo de 40 Mio ne doit jamais transiter
        # entierement par la RAM d'un RPi3.
        written = 0
        trop_gros = False
        with open(destination, "wb") as handle:
            while True:
                chunk = await upload.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    trop_gros = True
                    break
                handle.write(chunk)

        if trop_gros:
            destination.unlink(missing_ok=True)
            rejetees.append(f"{upload.filename} : depasse {settings.max_upload_mb} Mio")
            continue

        try:
            await run_in_threadpool(_ingest, project_id, upload.filename, destination, written)
            acceptees += 1
        except Exception as exc:
            destination.unlink(missing_ok=True)
            (settings.thumbs_dir(project_id) / name).unlink(missing_ok=True)
            (settings.work_dir(project_id) / name).unlink(missing_ok=True)
            rejetees.append(f"{upload.filename} : image illisible ({exc})")

    request.session["flash"] = {
        "acceptees": acceptees,
        "rejetees": rejetees,
    }
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def get_photo(photo_id: int) -> dict:
    row = db.fetch_one("SELECT * FROM photos WHERE id = ?", (photo_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Photo introuvable")
    return dict(row)


@router.post("/photos/{photo_id}/toggle")
async def toggle_photo(request: Request, photo_id: int):
    photo = get_photo(photo_id)
    nouvelle_valeur = 0 if photo["included"] else 1
    db.execute("UPDATE photos SET included = ? WHERE id = ?", (nouvelle_valeur, photo_id))
    photo["included"] = nouvelle_valeur
    return templates.TemplateResponse(
        request,
        "partials/photo_card.html",
        {"photo": photo, "seuil_nettete": imaging.SHARPNESS_FLOOR},
    )


@router.post("/photos/{photo_id}/delete")
async def delete_photo(photo_id: int):
    photo = get_photo(photo_id)
    project_id = photo["project_id"]
    for directory in (
        settings.photos_dir(project_id),
        settings.thumbs_dir(project_id),
        settings.work_dir(project_id),
    ):
        (directory / photo["filename"]).unlink(missing_ok=True)
    db.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/photos/auto-select")
async def auto_select(project_id: int, seuil: float = Form(imaging.SHARPNESS_FLOOR)):
    """Ecarte d'un coup les photos sous le seuil de nettete."""
    get_project(project_id)
    db.execute(
        "UPDATE photos SET included = CASE WHEN sharpness IS NOT NULL AND sharpness < ? "
        "THEN 0 ELSE 1 END WHERE project_id = ?",
        (seuil, project_id),
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _photo_file(photo: dict, directory: Path) -> FileResponse:
    path = directory / photo["filename"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Fichier absent du disque")
    # Les photos ne changent jamais apres l'ingestion : un cache long evite au
    # RPi de reservir les memes vignettes a chaque rafraichissement.
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/photos/{photo_id}/thumb")
async def photo_thumb(photo_id: int):
    photo = get_photo(photo_id)
    return _photo_file(photo, settings.thumbs_dir(photo["project_id"]))


@router.get("/photos/{photo_id}/full")
async def photo_full(photo_id: int):
    photo = get_photo(photo_id)
    return _photo_file(photo, settings.photos_dir(photo["project_id"]))
