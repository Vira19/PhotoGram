"""Execution d'un plan de reconstruction.

Le runner est volontairement synchrone et mono-job : sur un RPi3, lancer deux
reconstructions en parallele garantit l'OOM.  La serialisation est assuree en
amont par le worker, qui ne reclame qu'un job a la fois.
"""

from __future__ import annotations

import collections
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

from .plan import PlanContext, Step

WINDOWS = os.name == "nt"

#: subprocess n'expose cette constante que sous Windows ; la nommer ici rend
#: la branche lisible et testable depuis n'importe quel systeme.
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

#: Lignes de sortie conservees pour expliquer un echec. Un « code de sortie 1 »
#: seul n'apprend rien : c'est le message de l'outil qui compte.
LIGNES_ERREUR = 12

#: Periode de scrutation de la demande d'annulation (secondes).
CANCEL_POLL_INTERVAL = 2.0

#: Au-dela, on considere l'etape bloquee. 12 h laisse largement le temps a une
#: densification sur RPi3, tout en evitant qu'un job zombie occupe la file.
STEP_TIMEOUT_SECONDS = 12 * 3600


class Cancelled(Exception):
    """L'utilisateur a demande l'arret de la reconstruction."""


class StepFailed(Exception):
    def __init__(self, step: Step, message: str):
        super().__init__(message)
        self.step = step


class JobLogger:
    """Journal du job : fichier sur disque, en append et sans tampon long.

    L'interface web relit ce fichier a chaque rafraichissement ; il faut donc
    que les lignes y soient visibles immediatement.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)

    def write(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        for line in message.rstrip("\n").split("\n"):
            self._handle.write(f"[{stamp}] {line}\n")
        self._handle.flush()

    def raw(self, line: str) -> None:
        self._handle.write(line.rstrip("\n") + "\n")
        self._handle.flush()

    def rule(self, title: str) -> None:
        self.write("")
        self.write("=" * 70)
        self.write(title)
        self.write("=" * 70)

    def close(self) -> None:
        try:
            self._handle.close()
        except Exception:
            pass


def _subprocess_env(ctx: PlanContext) -> dict:
    env = dict(os.environ)
    # OpenMVG et OpenMVS s'appuient sur OpenMP : sans plafond, ils saturent les
    # quatre coeurs du RPi et rendent l'interface web inutilisable.
    env["OMP_NUM_THREADS"] = str(ctx.threads)
    env["OPENBLAS_NUM_THREADS"] = str(ctx.threads)
    return env


def run_command(
    argv: List[str],
    ctx: PlanContext,
    log: JobLogger,
    is_cancelled: Callable[[], bool],
) -> tuple:
    """Lance une commande et renvoie (code de sortie, dernieres lignes).

    La sortie complete part dans le journal ; les dernieres lignes sont
    conservees a part pour pouvoir expliquer un echec sans obliger a ouvrir le
    journal.
    """
    log.write("$ " + " ".join(argv))
    recentes = collections.deque(maxlen=LIGNES_ERREUR)

    process = subprocess.Popen(
        argv,
        cwd=str(ctx.job_dir),
        env=_subprocess_env(ctx),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        **_isolation_processus(),
    )

    deadline = time.monotonic() + STEP_TIMEOUT_SECONDS
    last_check = time.monotonic()

    try:
        assert process.stdout is not None
        for line in process.stdout:
            log.raw(line)
            if line.strip():
                recentes.append(line.rstrip())
            now = time.monotonic()
            if now - last_check >= CANCEL_POLL_INTERVAL:
                last_check = now
                if is_cancelled():
                    _terminate(process, log)
                    raise Cancelled()
                if now > deadline:
                    _terminate(process, log)
                    raise StepFailed(
                        Step("timeout"),
                        f"Etape interrompue apres {STEP_TIMEOUT_SECONDS // 3600} h.",
                    )
    finally:
        if process.stdout:
            process.stdout.close()

    return process.wait(), list(recentes)


def _isolation_processus() -> dict:
    """Options Popen isolant l'outil externe dans son propre groupe.

    OpenMVS et COLMAP essaiment des processus fils : tuer le seul processus
    lance laisserait des orphelins consommer la machine. Les deux systemes
    offrent un mecanisme de groupe, mais pas le meme.
    """
    if WINDOWS:
        return {"creationflags": CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate(process: subprocess.Popen, log: JobLogger) -> None:
    """Arret de l'outil externe et de toute sa descendance."""
    if WINDOWS:
        _terminate_windows(process, log)
    else:
        _terminate_posix(process, log)


def _terminate_windows(process: subprocess.Popen, log: JobLogger) -> None:
    # Windows n'a pas d'equivalent de killpg ; taskkill /T parcourt l'arbre de
    # processus, ce qui est le seul moyen fiable d'emporter les fils.
    log.write("Arret demande : taskkill sur l'arbre de processus.")
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as erreur:
        log.write(f"taskkill indisponible ({erreur}) : arret du seul processus principal.")
        process.kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def _terminate_posix(process: subprocess.Popen, log: JobLogger) -> None:
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        return
    log.write("Arret demande : envoi de SIGTERM au groupe de processus.")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    try:
        process.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass
    log.write("Le processus resiste : envoi de SIGKILL.")
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_step(
    step: Step,
    ctx: PlanContext,
    log: JobLogger,
    is_cancelled: Callable[[], bool],
) -> Optional[int]:
    """Execute une etape. Leve StepFailed si elle echoue et n'est pas optionnelle."""
    if step.func is not None:
        step.func(ctx, log.write)
        return None

    argv = step.resolve_argv(ctx)
    code, sortie = run_command(argv, ctx, log, is_cancelled)

    if code != 0 and step.repli is not None:
        # Certaines etapes ont une seconde chance : typiquement, retomber sur
        # le processeur quand l'acceleration graphique se derobe. Mieux vaut
        # une reconstruction lente qu'un echec.
        alternative = step.repli(ctx, "\n".join(sortie).lower())
        if alternative:
            log.write("")
            log.write("Nouvel essai avec des options de repli.")
            code, sortie = run_command(
                [str(part) for part in alternative], ctx, log, is_cancelled
            )

    if code != 0:
        raise StepFailed(step, _message_echec(step, code, sortie))
    return code


def _message_echec(step: Step, code: int, sortie: List[str]) -> str:
    """Message d'echec portant l'explication de l'outil, pas seulement son code."""
    message = f"« {step.name} » a echoue (code de sortie {code})."

    # Les lignes vraiment parlantes sont celles qui annoncent une erreur ; a
    # defaut, les dernieres emises font l'affaire.
    marquantes = [
        ligne for ligne in sortie
        if any(mot in ligne.lower() for mot in ("error", "erreur", "fail", "fatal", "cannot", "unable"))
    ]
    extrait = (marquantes or sortie)[-4:]
    if extrait:
        message += " " + " | ".join(extrait)

    for motif, conseil in INDICES:
        if any(motif in ligne.lower() for ligne in sortie):
            message += f"  → {conseil}"
            break

    return message


#: Messages d'outils frequents, et ce qu'il faut en faire. Un utilisateur ne
#: peut pas deviner que « SiftGPU not fully supported » se contourne par un
#: reglage : autant le lui dire au moment ou il en a besoin.
INDICES = (
    ("siftgpu", "L'extraction sur GPU a echoue. Forcez le processeur avec "
                "PHOTOGRAM_COLMAP_GPU=0 dans .env."),
    ("no gpu", "Aucun GPU exploitable detecte. Forcez le processeur avec "
               "PHOTOGRAM_COLMAP_GPU=0 dans .env."),
    ("opengl", "Le contexte graphique n'a pas pu etre cree. Forcez le "
               "processeur avec PHOTOGRAM_COLMAP_GPU=0 dans .env."),
    ("requires cuda", "Cette version de COLMAP est compilee sans CUDA : "
                      "choisissez le profil « Nuage epars seulement »."),
    ("out of memory", "Memoire insuffisante : reduisez PHOTOGRAM_WORK_MAX_DIM, "
                      "le nombre de photos, ou choisissez un profil plus leger."),
    ("no space left", "Plus de place sur le volume de donnees."),
    ("not found", "Un fichier attendu est introuvable : voir le journal complet."),
    ("unrecognised option", "Cette version de l'outil ne connait pas une option "
                            "employee. Le message complet est a signaler : les noms "
                            "d'options changent d'une version a l'autre."),
    ("unrecognized option", "Cette version de l'outil ne connait pas une option "
                            "employee. Le message complet est a signaler : les noms "
                            "d'options changent d'une version a l'autre."),
)
