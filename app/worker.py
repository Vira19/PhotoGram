"""Worker de reconstruction : consomme la file de jobs, une tache a la fois.

Processus separe du serveur web, pour trois raisons :

* une reconstruction sature les quatre coeurs pendant des heures ; la laisser
  dans le processus web rendrait l'interface inutilisable ;
* le service systemd du worker peut etre « nice » et depriorise en E/S, sans
  penaliser le web ;
* le jour ou le calcul demenage sur une machine plus costaude, il suffit de
  deplacer ce service et de lui donner acces au meme repertoire de donnees.

Lancement :  python -m app.worker
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from . import db, system
from .config import settings
from .pipeline.binaries import detect_toolchain
from .pipeline.plan import PlanContext, build_plan
from .pipeline.presets import get_preset
from .pipeline.runner import Cancelled, JobLogger, StepFailed, run_step

POLL_INTERVAL = 5.0

#: PID du lanceur, quand le worker est demarre par « python -m app.run ».
#: Sur un service systemd la variable est absente et le controle est inactif.
_PARENT_PID = int(os.environ.get("PHOTOGRAM_PARENT_PID", "0") or 0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker] %(message)s",
)
log = logging.getLogger("photogram.worker")

_stop_requested = False


def _handle_signal(signum, _frame):
    global _stop_requested
    log.info("Signal %s recu : arret du worker.", signum)
    _stop_requested = True


def _arret_demande() -> bool:
    """Le worker doit-il s'arreter : signal recu, ou lanceur disparu ?

    Le second cas couvre la fermeture du terminal sous « python -m app.run » :
    sans ce controle, le worker survivrait en orphelin, garderait la base
    ouverte et continuerait a consommer la machine sans que rien ne l'affiche.
    Sur les systemes POSIX, un processus orphelin est reattache a init, donc
    son PPID cesse de correspondre a celui du lanceur.
    """
    global _stop_requested
    if _stop_requested:
        return True
    if _PARENT_PID and not _parent_vivant(_PARENT_PID):
        log.info("Lanceur disparu : arret du worker.")
        _stop_requested = True
        return True
    return False


def _parent_vivant(pid: int) -> bool:
    """Le processus lanceur tourne-t-il toujours ?

    Sous Windows, rien ne reattache un orphelin : le PPID garde son ancienne
    valeur, et on doit interroger le processus directement. On n'utilise
    surtout pas os.kill(pid, 0), qui sous Windows ne teste rien mais **tue**
    le processus vise.
    """
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True  # dans le doute, on continue de travailler
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    return os.getppid() == pid


def claim_job() -> Optional[dict]:
    """Reserve le plus ancien job en attente.

    La transaction immediate garantit qu'un second worker (ou un futur worker
    distant) ne puisse pas reclamer le meme job.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, error = '', "
            "attempts = attempts + 1 WHERE id = ?",
            (db.now(), row["id"]),
        )
        return dict(row)


def is_cancelled(job_id: int) -> bool:
    row = db.fetch_one("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,))
    return bool(row and row["cancel_requested"])


def finish_job(job_id: int, status: str, error: str = "") -> None:
    db.execute(
        "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
        (status, error[:2000], db.now(), job_id),
    )


def register_steps(job_id: int, steps: list) -> None:
    with db.cursor() as conn:
        conn.execute("DELETE FROM job_steps WHERE job_id = ?", (job_id,))
        conn.executemany(
            "INSERT INTO job_steps (job_id, position, name, status) VALUES (?, ?, ?, 'pending')",
            [(job_id, index, step.name) for index, step in enumerate(steps)],
        )
        conn.execute("UPDATE jobs SET step_total = ? WHERE id = ?", (len(steps), job_id))


def mark_step(job_id: int, position: int, status: str, **fields) -> None:
    assignments = ["status = ?"]
    params = [status]
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        params.append(value)
    params.extend([job_id, position])
    db.execute(
        f"UPDATE job_steps SET {', '.join(assignments)} WHERE job_id = ? AND position = ?",
        tuple(params),
    )


def collect_photos(project_id: int) -> list:
    """Copies de travail des photos retenues, dans un ordre stable."""
    rows = db.fetch_all(
        "SELECT filename FROM photos WHERE project_id = ? AND included = 1 ORDER BY filename",
        (project_id,),
    )
    work_dir = settings.work_dir(project_id)
    return [work_dir / row["filename"] for row in rows if (work_dir / row["filename"]).is_file()]


def fallback_focal_px(project_id: int) -> Optional[float]:
    """Focale de repli, en pixels, si aucune photo ne porte de focale EXIF.

    La regle empirique d'OpenMVG (1,2 x le cote long) donne un point de depart
    raisonnable pour un capteur de telephone ou de compact ; le SfM affine
    ensuite la valeur.
    """
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM photos WHERE project_id = ? AND included = 1 AND focal_mm IS NOT NULL",
        (project_id,),
    )
    if row and row["n"]:
        return None

    dims = db.fetch_one(
        "SELECT MAX(MAX(width, height)) AS longest FROM photos WHERE project_id = ? AND included = 1",
        (project_id,),
    )
    longest = (dims["longest"] if dims else 0) or 0
    if not longest:
        return None
    # La focale s'applique aux copies de travail, pas aux originaux.
    scale = min(1.0, settings.work_max_dim / float(longest))
    return 1.2 * longest * scale


def record_artifacts(job_id: int, out_dir: Path) -> None:
    if not out_dir.is_dir():
        return
    rows = []
    for path in sorted(out_dir.iterdir()):
        if not path.is_file():
            continue
        kind = {
            ".ply": "nuage",
            ".obj": "maillage",
            ".mtl": "materiau",
            ".png": "texture",
            ".jpg": "texture",
        }.get(path.suffix.lower(), "fichier")
        rows.append((job_id, kind, path.name, path.stat().st_size, db.now()))
    with db.cursor() as conn:
        conn.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))
        conn.executemany(
            "INSERT INTO artifacts (job_id, kind, filename, bytes, created_at) VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def cleanup_intermediates(ctx: PlanContext, logger: JobLogger) -> None:
    """Supprime les fichiers de travail une fois les resultats mis de cote.

    Une reconstruction laisse facilement plusieurs Gio derriere elle ; sur une
    carte SD, les garder par defaut remplirait le support en quelques jobs.
    """
    if settings.keep_intermediates:
        logger.write("Fichiers intermediaires conserves (PHOTOGRAM_KEEP_INTERMEDIATES=1).")
        return

    freed = 0
    for directory in (ctx.mvg_dir, ctx.mvs_dir, ctx.images_dir):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file():
                freed += path.stat().st_size
        shutil.rmtree(directory, ignore_errors=True)
    logger.write(f"Fichiers intermediaires supprimes ({system.human_bytes(freed)} liberes).")


def process_job(job: dict) -> None:
    job_id = job["id"]
    project_id = job["project_id"]
    preset = get_preset(job["preset"])
    job_dir = settings.job_dir(project_id, job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    logger = JobLogger(job_dir / "job.log")
    tools = detect_toolchain()
    ctx = PlanContext(
        job_id=job_id,
        project_id=project_id,
        job_dir=job_dir,
        preset=preset,
        tools=tools,
        threads=settings.threads,
        source_photos=collect_photos(project_id),
        focal_px=fallback_focal_px(project_id),
        backend=tools.choose_backend(settings.backend, preset.sparse_only),
    )

    try:
        logger.rule(f"Job #{job_id} — profil « {preset.label} »")
        logger.write(f"{len(ctx.source_photos)} photos retenues, {ctx.threads} threads.")

        snap = system.snapshot()
        logger.write(
            "Machine : {cpus} coeurs, {ram} de RAM dont {libre} disponibles, "
            "{disque} libres sur le volume de donnees.".format(
                cpus=snap["cpus"],
                ram=system.human_bytes(snap["memoire"]["total"]),
                libre=system.human_bytes(snap["memoire"]["available"]),
                disque=system.human_bytes(snap["disque"]["free"]),
            )
        )
        for warning in system.memory_warnings(preset.key):
            logger.write("ATTENTION : " + warning)

        logger.write(f"Chaine utilisee : {ctx.backend}.")

        if ctx.backend == "colmap" and not preset.sparse_only:
            # La densification de COLMAP exige CUDA : mieux vaut le dire net
            # que de laisser tourner des heures pour rien.
            raise StepFailed(
                None,
                "COLMAP ne sait produire qu'un nuage epars sans GPU NVIDIA. "
                "Choisissez le profil « Nuage epars seulement », ou installez "
                "OpenMVG et OpenMVS pour obtenir un maillage.",
            )

        missing = ctx.tools.missing_for(ctx.backend, preset.sparse_only)
        if missing:
            raise StepFailed(
                None,
                "Binaires manquants : " + ", ".join(missing)
                + ". Voir scripts/install_pipeline.sh.",
            )

        steps = build_plan(ctx)
        register_steps(job_id, steps)

        for position, step in enumerate(steps):
            if is_cancelled(job_id) or _arret_demande():
                raise Cancelled()

            db.execute(
                "UPDATE jobs SET current_step = ?, step_index = ? WHERE id = ?",
                (step.name, position, job_id),
            )
            mark_step(job_id, position, "running", started_at=db.now())
            logger.rule(f"Etape {position + 1}/{len(steps)} : {step.name}")

            started = time.monotonic()
            try:
                code = run_step(
                    step, ctx, logger,
                    lambda: is_cancelled(job_id) or _arret_demande(),
                )
            except Cancelled:
                mark_step(job_id, position, "cancelled", finished_at=db.now())
                raise
            except StepFailed as exc:
                duration = time.monotonic() - started
                mark_step(
                    job_id, position,
                    "skipped" if step.optional else "failed",
                    finished_at=db.now(), duration_s=round(duration, 1),
                )
                if step.optional:
                    logger.write(f"Etape optionnelle echouee, on continue : {exc}")
                    continue
                raise
            except Exception as exc:  # etape Python en erreur
                duration = time.monotonic() - started
                mark_step(job_id, position, "failed", finished_at=db.now(), duration_s=round(duration, 1))
                raise StepFailed(step, f"« {step.name} » : {exc}") from exc

            duration = time.monotonic() - started
            mark_step(
                job_id, position, "done",
                finished_at=db.now(), duration_s=round(duration, 1), exit_code=code,
            )
            logger.write(f"Etape terminee en {duration / 60:.1f} min.")

        record_artifacts(job_id, ctx.out_dir)
        cleanup_intermediates(ctx, logger)
        db.execute("UPDATE jobs SET current_step = 'Termine' WHERE id = ?", (job_id,))
        finish_job(job_id, "done")
        logger.rule("Reconstruction terminee avec succes.")

    except Cancelled:
        if _stop_requested and not is_cancelled(job_id):
            logger.rule("Worker arrete : la reconstruction est remise en file.")
            db.execute(
                "UPDATE jobs SET status = 'queued', started_at = NULL, step_index = 0, "
                "current_step = '' WHERE id = ?",
                (job_id,),
            )
        else:
            logger.rule("Reconstruction annulee a la demande de l'utilisateur.")
            record_artifacts(job_id, ctx.out_dir)
            finish_job(job_id, "cancelled", "Annule par l'utilisateur.")
    except StepFailed as exc:
        logger.rule(f"ECHEC : {exc}")
        record_artifacts(job_id, ctx.out_dir)
        finish_job(job_id, "failed", str(exc))
    except Exception as exc:  # pragma: no cover - filet de securite
        log.exception("Job #%s : erreur inattendue", job_id)
        logger.rule(f"ECHEC INATTENDU : {exc}")
        finish_job(job_id, "failed", f"Erreur inattendue : {exc}")
    finally:
        logger.close()


#: Au-dela, on cesse de relancer un job qui meurt a chaque tentative.
MAX_TENTATIVES = 2


def requeue_orphans() -> None:
    """Traite les jobs restes « running » apres un arret brutal.

    Le worker etant unique, tout job marque en cours au demarrage est un
    orphelin : coupure de courant, reboot, ou OOM killer. On le relance une
    fois, car une coupure n'a rien a voir avec la reconstruction elle-meme.
    Au-dela, on arrete : un job qui tue le worker a chaque passage le tuerait
    indefiniment, et la file ne redemarrerait jamais.
    """
    for row in db.fetch_all("SELECT id, attempts FROM jobs WHERE status = 'running'"):
        if row["attempts"] >= MAX_TENTATIVES:
            log.error("Job #%s interrompu %s fois : abandon.", row["id"], row["attempts"])
            db.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (
                    db.now(),
                    f"Interrompu {row['attempts']} fois sans terminer. Le worker a "
                    "probablement ete tue par manque de memoire : reduisez le nombre "
                    "de photos, choisissez un profil plus leger, ou ajoutez du swap.",
                    row["id"],
                ),
            )
            continue

        log.warning("Job #%s retrouve en cours au demarrage : remis en file.", row["id"])
        db.execute(
            "UPDATE jobs SET status = 'queued', started_at = NULL, step_index = 0, "
            "current_step = '', error = '' WHERE id = ?",
            (row["id"],),
        )


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    db.init_db()
    settings.ensure_dirs()
    requeue_orphans()

    tools = detect_toolchain()
    if tools.missing():
        log.warning("Binaires du pipeline manquants : %s", ", ".join(tools.missing()))
    else:
        log.info("Chaine detectee : OpenMVG %s + OpenMVS.",
                 "2.x" if tools.modern_openmvg else "1.x")

    log.info("Worker pret (donnees : %s, %s threads).", settings.data_dir, settings.threads)

    while not _arret_demande():
        job = claim_job()
        if job is None:
            time.sleep(POLL_INTERVAL)
            continue
        log.info("Job #%s demarre (projet %s, profil %s).", job["id"], job["project_id"], job["preset"])
        process_job(job)
        log.info("Job #%s termine.", job["id"])

    log.info("Worker arrete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
