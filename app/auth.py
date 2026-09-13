"""Authentification : un unique mot de passe partage, pensee pour un LAN prive.

Pas de comptes ni de base d'utilisateurs : le site n'est pas cense sortir du
reseau local.  Si un jour il est expose sur Internet, c'est cette brique qu'il
faudra remplacer en premier (et elle seule).
"""

from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import RedirectResponse

from .config import settings

SESSION_KEY = "auth"


def check_password(candidate: str) -> bool:
    if not settings.password:
        return False
    return hmac.compare_digest(candidate, settings.password)


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def login(request: Request) -> None:
    request.session[SESSION_KEY] = True


def logout(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)


def redirect_to_login(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target = target + "?" + request.url.query
    return RedirectResponse(f"/login?next={target}", status_code=303)
