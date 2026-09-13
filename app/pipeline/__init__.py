"""Pipeline de reconstruction (OpenMVG + OpenMVS, ou COLMAP)."""

from __future__ import annotations

from typing import Dict

from .binaries import Toolchain, detect_toolchain
from .plan import Step, build_plan
from .presets import PRESETS, Preset, get_preset


def preset_availability(tools: Toolchain, preference: str = "auto") -> Dict[str, dict]:
    """Pour chaque profil, dit s'il peut tourner ici et pourquoi non le cas echeant.

    Permet a l'interface de decourager un profil voue a l'echec avant de
    mobiliser le worker, plutot que de le laisser echouer apres coup.
    """
    etat = {}
    for cle, preset in PRESETS.items():
        backend = tools.choose_backend(preference, preset.sparse_only)
        manquants = tools.missing_for(backend, preset.sparse_only)

        if backend == "colmap" and not preset.sparse_only:
            etat[cle] = {
                "disponible": False,
                "backend": backend,
                "motif": (
                    "COLMAP ne produit qu'un nuage epars sans GPU NVIDIA. "
                    "Installez OpenMVG et OpenMVS pour obtenir un maillage."
                ),
            }
        elif manquants:
            etat[cle] = {
                "disponible": False,
                "backend": backend,
                "motif": "Binaires manquants : " + ", ".join(manquants),
            }
        else:
            etat[cle] = {"disponible": True, "backend": backend, "motif": ""}
    return etat


__all__ = [
    "Toolchain",
    "detect_toolchain",
    "PRESETS",
    "Preset",
    "get_preset",
    "Step",
    "build_plan",
    "preset_availability",
]
