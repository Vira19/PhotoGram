"""Etat de la machine : memoire, disque, charge.

Les sondes materielles elles-memes vivent dans app.hardware, sans dependance a
la configuration ; ce module y ajoute ce qui a besoin de connaitre le
repertoire de donnees.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import hardware
from .config import settings


def memory() -> dict:
    info = hardware.memoire()
    total = info["total"]
    available = info["available"]
    return {
        "total": total,
        "available": available,
        "used": max(0, total - available),
        "swap_total": info["swap_total"],
        "swap_used": max(0, info["swap_total"] - info["swap_free"]),
        "percent": round(100 * (total - available) / total, 1) if total and available else 0.0,
        # Faux sur un systeme dont on ne sait lire que le total : l'interface
        # doit alors afficher « inconnu » plutot qu'un zero trompeur.
        "complet": info["complet"],
    }


def disk() -> dict:
    target = settings.data_dir if settings.data_dir.exists() else Path(".")
    usage = shutil.disk_usage(target)
    return {
        "total": usage.total,
        "free": usage.free,
        "used": usage.used,
        "percent": round(100 * usage.used / usage.total, 1) if usage.total else 0.0,
    }


def load_average() -> tuple:
    return hardware.charge()


def cpu_temperature() -> float:
    return hardware.temperature_cpu()


def human_bytes(value: float) -> str:
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if abs(value) < 1024.0 or unit == "Tio":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} Tio"


def snapshot() -> dict:
    return {
        "memoire": memory(),
        "disque": disk(),
        "charge": load_average(),
        "temperature": cpu_temperature(),
        "cpus": os.cpu_count() or 1,
        "systeme": os.name,
    }


def memory_warnings(preset_key: str) -> list:
    """Avertissements a inscrire au journal avant de lancer un job."""
    warnings = []
    mem = memory()
    total_mb = mem["total"] / (1024 * 1024)
    swap_mb = mem["swap_total"] / (1024 * 1024)
    free_mb = disk()["free"] / (1024 * 1024)

    if total_mb and total_mb < 1536 and preset_key not in ("sparse",):
        warnings.append(
            f"Seulement {total_mb:.0f} Mio de RAM detectes pour un profil « {preset_key} » : "
            "la densification risque d'etre tuee par l'OOM killer."
        )
    if total_mb and total_mb < 1536 and swap_mb < 1024 and mem["complet"]:
        warnings.append(
            f"Swap de {swap_mb:.0f} Mio seulement. Sur RPi3, passez a 2 Gio "
            "(CONF_SWAPSIZE=2048 dans /etc/dphys-swapfile)."
        )
    if free_mb < 2048:
        warnings.append(
            f"Il ne reste que {free_mb:.0f} Mio sur le volume de donnees : "
            "le pipeline peut manquer de place."
        )
    return warnings
