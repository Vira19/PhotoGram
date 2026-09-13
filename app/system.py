"""Sonde systeme : memoire, swap, disque, charge.

Utile a deux endroits : la page d'etat, et l'avertissement inscrit en tete de
journal avant une densification, ou l'OOM killer est le risque principal.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import settings


def _meminfo() -> dict:
    values = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    values[key.strip()] = int(parts[0]) * 1024  # kB -> octets
    except OSError:
        pass
    return values


def memory() -> dict:
    info = _meminfo()
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    return {
        "total": total,
        "available": available,
        "used": max(0, total - available),
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
        "percent": round(100 * (total - available) / total, 1) if total else 0.0,
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
    try:
        return os.getloadavg()
    except (OSError, AttributeError):
        return (0.0, 0.0, 0.0)


def cpu_temperature() -> float:
    """Temperature du SoC en degres Celsius, 0 si indisponible.

    Un RPi3 sans dissipateur throttle des 80 degres : sur un calcul de
    plusieurs heures, c'est une cause de lenteur qu'il vaut mieux voir.
    """
    for path in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return round(int(handle.read().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            continue
    return 0.0


def human_bytes(value: float) -> str:
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if abs(value) < 1024.0 or unit == "Tio":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} Tio"


def snapshot() -> dict:
    mem = memory()
    dsk = disk()
    return {
        "memoire": mem,
        "disque": dsk,
        "charge": load_average(),
        "temperature": cpu_temperature(),
        "cpus": os.cpu_count() or 1,
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
    if total_mb and total_mb < 1536 and swap_mb < 1024:
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
