"""Execution d'un plan de reconstruction.

Le runner est volontairement synchrone et mono-job : sur un RPi3, lancer deux
reconstructions en parallele garantit l'OOM.  La serialisation est assuree en
amont par le worker, qui ne reclame qu'un job a la fois.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

from .plan import PlanContext, Step

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
) -> int:
    """Lance une commande, recopie sa sortie dans le journal, gere l'annulation."""
    log.write("$ " + " ".join(argv))

    process = subprocess.Popen(
        argv,
        cwd=str(ctx.job_dir),
        env=_subprocess_env(ctx),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        # Groupe de processus dedie : OpenMVS essaime des fils, et seul un
        # signal au groupe entier garantit qu'une annulation les emporte tous.
        start_new_session=True,
    )

    deadline = time.monotonic() + STEP_TIMEOUT_SECONDS
    last_check = time.monotonic()

    try:
        assert process.stdout is not None
        for line in process.stdout:
            log.raw(line)
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

    return process.wait()


def _terminate(process: subprocess.Popen, log: JobLogger) -> None:
    """Arret propre puis brutal du groupe de processus."""
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
    code = run_command(argv, ctx, log, is_cancelled)
    if code != 0:
        raise StepFailed(step, f"« {step.name} » a echoue (code de sortie {code}).")
    return code
