"""Point d'entree de l'application web.

Le serveur ne fait que de l'I/O legere : reception des photos, vignettes, et
lecture de l'etat des jobs.  Tout le calcul lourd vit dans app/worker.py.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, auth, db, system
from .config import settings
from .pipeline.binaries import detect_toolchain
from .routes import auth_routes, jobs, projects
from .templating import TEMPLATE_DIR, templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [web] %(message)s")
log = logging.getLogger("photogram.web")

#: Chemins accessibles sans etre authentifie.
PUBLIC_PREFIXES = ("/login", "/logout", "/static", "/health")


def create_app() -> FastAPI:
    db.init_db()
    settings.ensure_dirs()

    application = FastAPI(title="PhotoGram", version=__version__, docs_url=None, redoc_url=None)

    # Sans cle de signature configuree, mieux vaut une cle ephemere qu'un
    # secret en dur : les sessions ne survivront pas a un redemarrage, ce qui
    # est le comportement le moins dangereux.
    secret = settings.secret_key
    if not secret:
        import secrets as _secrets
        secret = _secrets.token_hex(32)
        log.warning("PHOTOGRAM_SECRET_KEY absent : cle de session ephemere generee.")

    # Ajoute en premier, donc execute en dernier : a ce stade SessionMiddleware
    # a deja peuple request.session.
    @application.middleware("http")
    async def require_authentication(request: Request, call_next):
        path = request.url.path
        if path.startswith(PUBLIC_PREFIXES) or auth.is_logged_in(request):
            return await call_next(request)
        if request.headers.get("x-requested-with") == "fetch":
            # Une requete de fond ne doit pas recevoir la page de connexion :
            # le client JavaScript interprete le 401 et redirige lui-meme.
            return JSONResponse({"detail": "Session expiree"}, status_code=401)
        return auth.redirect_to_login(request)

    application.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="photogram_session",
        max_age=14 * 24 * 3600,
        same_site="lax",
        https_only=False,  # LAN prive, pas de TLS
    )

    static_dir = TEMPLATE_DIR.parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    application.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    application.include_router(auth_routes.router)
    application.include_router(projects.router)
    application.include_router(jobs.router)

    @application.get("/health")
    async def health(request: Request):
        tools = detect_toolchain()
        payload = {
            "version": __version__,
            "systeme": system.snapshot(),
            "pipeline": tools.summary(),
            "configuration": settings.problems(),
            "worker": worker_status(),
        }
        if "application/json" in (request.headers.get("accept") or ""):
            return JSONResponse(payload)
        return templates.TemplateResponse(
            request,
            "health.html",
            {"sante": payload, "connecte": auth.is_logged_in(request)},
        )

    @application.exception_handler(404)
    async def not_found(request: Request, _exc):
        if request.url.path.startswith("/static"):
            return JSONResponse({"detail": "Introuvable"}, status_code=404)
        if not auth.is_logged_in(request):
            return auth.redirect_to_login(request)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"titre": "Page introuvable", "message": "Cette adresse n'existe pas."},
            status_code=404,
        )

    return application


def worker_status() -> dict:
    """Etat de la file, seul indicateur fiable de la vitalite du worker.

    Un job « en cours » depuis longtemps sans progression signale en general
    un worker tue par l'OOM killer.
    """
    counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
    for row in db.fetch_all("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        counts[row["status"]] = row["n"]
    running = db.fetch_one(
        "SELECT id, project_id, current_step, started_at FROM jobs WHERE status = 'running' LIMIT 1"
    )
    return {"file": counts, "en_cours": dict(running) if running else None}


app = create_app()
