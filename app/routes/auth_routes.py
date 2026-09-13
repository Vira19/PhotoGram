"""Connexion / deconnexion (mot de passe unique)."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import auth
from ..config import settings
from ..templating import templates

router = APIRouter()

#: Tentatives ratees par adresse IP : { ip: (compteur, horodatage) }.
#: Un LAN prive ne justifie pas un vrai anti-bruteforce, mais un delai
#: progressif coute trois lignes et ferme la porte aux scans opportunistes.
_failures: dict = {}
_LOCKOUT_AFTER = 5
_LOCKOUT_SECONDS = 60


def _is_locked(ip: str) -> int:
    count, last = _failures.get(ip, (0, 0.0))
    if count < _LOCKOUT_AFTER:
        return 0
    remaining = int(_LOCKOUT_SECONDS - (time.time() - last))
    if remaining <= 0:
        _failures.pop(ip, None)
        return 0
    return remaining


def _safe_next(target: Optional[str]) -> str:
    """N'accepte qu'un chemin interne, pour eviter une redirection ouverte."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@router.get("/login")
async def login_form(request: Request, next: str = "/"):
    if auth.is_logged_in(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": _safe_next(next),
            "erreur": None,
            "config_incomplete": settings.problems(),
        },
    )


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form("/"),
):
    ip = request.client.host if request.client else "?"
    locked = _is_locked(ip)
    if locked:
        erreur = f"Trop de tentatives. Reessayez dans {locked} s."
    elif auth.check_password(password):
        _failures.pop(ip, None)
        auth.login(request)
        return RedirectResponse(_safe_next(next), status_code=303)
    else:
        count, _ = _failures.get(ip, (0, 0.0))
        _failures[ip] = (count + 1, time.time())
        erreur = "Mot de passe incorrect."

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": _safe_next(next),
            "erreur": erreur,
            "config_incomplete": settings.problems(),
        },
        status_code=401,
    )


@router.post("/logout")
async def logout(request: Request):
    auth.logout(request)
    return RedirectResponse("/login", status_code=303)
