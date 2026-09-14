"""Sondes materielles portables.

Volontairement sans dependance au reste du projet : le lanceur doit pouvoir
interroger la machine avant que la configuration ne soit figee, et un import
de app.config a ce moment-la fixerait des valeurs issues d'un .env pas encore
ecrit.

Aucune bibliotheque tierce : psutil ferait le travail, mais c'est une extension
native de plus a installer, exactement ce qu'on cherche a eviter sur un Pi.
"""

from __future__ import annotations

import os
import subprocess

WINDOWS = os.name == "nt"


def _memoire_linux() -> dict:
    valeurs = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for ligne in handle:
                cle, _, reste = ligne.partition(":")
                morceaux = reste.split()
                if morceaux:
                    valeurs[cle.strip()] = int(morceaux[0]) * 1024
    except OSError:
        return {}
    if "MemTotal" not in valeurs:
        return {}
    return {
        "total": valeurs.get("MemTotal", 0),
        "available": valeurs.get("MemAvailable", 0),
        "swap_total": valeurs.get("SwapTotal", 0),
        "swap_free": valeurs.get("SwapFree", 0),
        "complet": True,
    }


def _memoire_windows() -> dict:
    try:
        import ctypes

        class _Statut(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        statut = _Statut()
        statut.dwLength = ctypes.sizeof(_Statut)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(statut)):
            return {}
        # Windows ne distingue pas un « swap » : le fichier d'echange est
        # compte avec la memoire physique dans TotalPageFile. La difference en
        # est une approximation suffisante pour les avertissements affiches.
        return {
            "total": int(statut.ullTotalPhys),
            "available": int(statut.ullAvailPhys),
            "swap_total": max(0, int(statut.ullTotalPageFile) - int(statut.ullTotalPhys)),
            "swap_free": max(0, int(statut.ullAvailPageFile) - int(statut.ullAvailPhys)),
            "complet": True,
        }
    except Exception:
        return {}


def _memoire_macos() -> dict:
    total = _total_par_sysconf()
    if not total:
        return {}
    # vm_stat est le seul point d'entree simple pour la memoire libre sur
    # macOS ; son absence n'est pas bloquante, on perd juste le detail.
    try:
        sortie = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5, check=True
        ).stdout
        taille_page = 4096
        pages = {}
        for ligne in sortie.splitlines():
            if "page size of" in ligne:
                taille_page = int(ligne.split("page size of")[1].split()[0])
            elif ":" in ligne:
                cle, _, valeur = ligne.partition(":")
                valeur = valeur.strip().rstrip(".")
                if valeur.isdigit():
                    pages[cle.strip()] = int(valeur)
        libre = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0)) * taille_page
        return {"total": total, "available": libre, "swap_total": 0, "swap_free": 0, "complet": True}
    except Exception:
        return {"total": total, "available": 0, "swap_total": 0, "swap_free": 0, "complet": False}


def _total_par_sysconf() -> int:
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, KeyError):
        pass
    return 0


def memoire() -> dict:
    """Memoire physique, en octets.

    ``complet`` indique si le detail (disponible, swap) est fiable : sur un
    systeme non reconnu, seul le total l'est, et l'interface doit le dire
    plutot que d'afficher zero.
    """
    for sonde in (_memoire_linux, _memoire_windows, _memoire_macos):
        if sonde is _memoire_windows and not WINDOWS:
            continue
        resultat = sonde()
        if resultat:
            return resultat

    total = _total_par_sysconf()
    return {"total": total, "available": 0, "swap_total": 0, "swap_free": 0, "complet": False}


def charge() -> tuple:
    """Charge moyenne. Absente de Windows, ou l'on renvoie des zeros."""
    try:
        return os.getloadavg()
    except (OSError, AttributeError):
        return (0.0, 0.0, 0.0)


def temperature_cpu() -> float:
    """Temperature du SoC en degres Celsius, 0 si indisponible.

    Un RPi3 sans dissipateur bride sa frequence des 80 degres ; sur un calcul
    de plusieurs heures, c'est une cause de lenteur qu'il vaut mieux voir.
    Le fichier n'existe que sous Linux.
    """
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r", encoding="utf-8") as handle:
            return round(int(handle.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return 0.0
