"""Reconstructions : mise en file, suivi, resultats."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

from .. import db
from ..config import settings
from ..pipeline import detect_toolchain, preset_availability
from ..pipeline.presets import PRESETS, get_preset
from ..templating import templates

router = APIRouter()

#: Taille de la queue de journal renvoyee a l'interface. Assez pour diagnostiquer,
#: assez peu pour ne pas saturer le lien Wi-Fi d'un RPi a chaque rafraichissement.
LOG_TAIL_BYTES = 40_000

VIEWABLE_SUFFIXES = {".ply", ".obj"}


def get_job(job_id: int) -> dict:
    row = db.fetch_one(
        "SELECT j.*, p.name AS project_name FROM jobs j "
        "JOIN projects p ON p.id = j.project_id WHERE j.id = ?",
        (job_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Reconstruction introuvable")
    return dict(row)


@router.post("/projects/{project_id}/jobs")
async def enqueue_job(project_id: int, preset: str = Form("")):
    projet = db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if projet is None:
        raise HTTPException(status_code=404, detail="Projet introuvable")

    retenues = db.fetch_one(
        "SELECT COUNT(*) AS n FROM photos WHERE project_id = ? AND included = 1",
        (project_id,),
    )["n"]
    if retenues < 5:
        raise HTTPException(
            status_code=400,
            detail="Il faut au moins 5 photos retenues pour tenter une reconstruction.",
        )
    if retenues > settings.max_photos:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{retenues} photos retenues alors que la limite est de {settings.max_photos}. "
                "Ecartez-en ou relevez PHOTOGRAM_MAX_PHOTOS."
            ),
        )

    if preset not in PRESETS:
        preset = projet["preset"]

    # Inutile de mobiliser le worker pour un profil que la machine ne sait pas
    # honorer : autant le dire tout de suite.
    etat = preset_availability(detect_toolchain(), settings.backend)[preset]
    if not etat["disponible"]:
        raise HTTPException(status_code=400, detail=etat["motif"])

    job_id = db.execute(
        "INSERT INTO jobs (project_id, status, preset, photo_count, created_at) "
        "VALUES (?, 'queued', ?, ?, ?)",
        (project_id, preset, retenues, db.now()),
    )
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


def _job_context(request: Request, job_id: int) -> dict:
    job = get_job(job_id)
    steps = db.fetch_all(
        "SELECT * FROM job_steps WHERE job_id = ? ORDER BY position", (job_id,)
    )
    artifacts = db.fetch_all(
        "SELECT * FROM artifacts WHERE job_id = ? ORDER BY filename", (job_id,)
    )
    return {
        "request": request,
        "job": job,
        "steps": steps,
        "artifacts": artifacts,
        "preset": get_preset(job["preset"]),
        "en_cours": job["status"] in ("queued", "running"),
        "visionnables": [a for a in artifacts if Path(a["filename"]).suffix.lower() in VIEWABLE_SUFFIXES],
    }


@router.get("/jobs/{job_id}")
async def job_detail(request: Request, job_id: int):
    return templates.TemplateResponse(request, "job.html", _job_context(request, job_id))


@router.get("/jobs/{job_id}/etat")
async def job_state(request: Request, job_id: int):
    """Fragment rafraichi par HTMX pendant qu'une reconstruction tourne."""
    return templates.TemplateResponse(request, "partials/job_state.html", _job_context(request, job_id))


@router.get("/jobs/{job_id}/journal")
async def job_log(job_id: int):
    job = get_job(job_id)
    path = settings.job_dir(job["project_id"], job_id) / "job.log"
    if not path.is_file():
        return PlainTextResponse("Le journal n'a pas encore ete cree.")

    size = path.stat().st_size
    with open(path, "rb") as handle:
        if size > LOG_TAIL_BYTES:
            handle.seek(size - LOG_TAIL_BYTES)
            handle.readline()  # on jette la ligne tronquee
        contenu = handle.read().decode("utf-8", errors="replace")

    entete = "" if size <= LOG_TAIL_BYTES else f"[... {size - LOG_TAIL_BYTES} octets plus anciens omis ...]\n"
    return PlainTextResponse(entete + contenu)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    job = get_job(job_id)
    if job["status"] == "queued":
        # Jamais reclame par le worker : on peut trancher tout de suite.
        db.execute(
            "UPDATE jobs SET status = 'cancelled', error = 'Annule avant demarrage.', "
            "finished_at = ? WHERE id = ?",
            (db.now(), job_id),
        )
    elif job["status"] == "running":
        # Le worker verra le drapeau et coupera le processus en cours.
        db.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/jobs/{job_id}/delete")
async def delete_job(job_id: int):
    import shutil

    job = get_job(job_id)
    if job["status"] == "running":
        raise HTTPException(status_code=400, detail="Annulez la reconstruction avant de la supprimer.")
    shutil.rmtree(settings.job_dir(job["project_id"], job_id), ignore_errors=True)
    db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    return RedirectResponse(f"/projects/{job['project_id']}", status_code=303)


def _artifact_path(job: dict, filename: str) -> Path:
    """Resout un nom de fichier de resultat, en refusant toute echappee.

    Le nom vient de l'URL : on verifie qu'il correspond bien a un resultat
    enregistre en base, puis que le chemin resolu reste dans le dossier du job.
    """
    known = db.fetch_one(
        "SELECT filename FROM artifacts WHERE job_id = ? AND filename = ?",
        (job["id"], filename),
    )
    if known is None:
        raise HTTPException(status_code=404, detail="Fichier inconnu pour cette reconstruction")

    out_dir = (settings.job_dir(job["project_id"], job["id"]) / "out").resolve()
    path = (out_dir / filename).resolve()
    if not str(path).startswith(str(out_dir)) or not path.is_file():
        raise HTTPException(status_code=404, detail="Fichier absent du disque")
    return path


@router.get("/jobs/{job_id}/fichiers/{filename}")
async def download_artifact(job_id: int, filename: str, inline: int = 0):
    job = get_job(job_id)
    path = _artifact_path(job, filename)
    # Les .obj referencent leur .mtl, qui reference la texture : servis en
    # ligne, ils doivent garder leur nom exact pour que la visionneuse suive.
    disposition = "inline" if inline else "attachment"
    return FileResponse(
        path,
        filename=filename,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.get("/jobs/{job_id}/visionneuse")
async def viewer(request: Request, job_id: int, fichier: Optional[str] = None):
    job = get_job(job_id)
    artifacts = db.fetch_all(
        "SELECT * FROM artifacts WHERE job_id = ? ORDER BY filename", (job_id,)
    )
    visionnables = [a for a in artifacts if Path(a["filename"]).suffix.lower() in VIEWABLE_SUFFIXES]
    if not visionnables:
        raise HTTPException(status_code=404, detail="Aucun modele visualisable pour cette reconstruction")

    noms = [a["filename"] for a in visionnables]
    choisi = fichier if fichier in noms else noms[0]
    _artifact_path(job, choisi)  # valide le chemin avant de l'exposer au JS

    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "job": job,
            "visionnables": visionnables,
            "choisi": choisi,
            "extension": Path(choisi).suffix.lower().lstrip("."),
        },
    )
