"""Pipeline de reconstruction (OpenMVG + OpenMVS, ou COLMAP)."""

from __future__ import annotations

from typing import Dict

from .binaries import Toolchain, detect_toolchain
from .plan import Step, build_plan
from .presets import PRESETS, Preset, get_preset


def resume_chaine(tools: Toolchain) -> tuple:
    """Etat de la chaine de reconstruction : (niveau, lignes a afficher).

    Le niveau vaut « complet », « partiel » ou « absent ». Un resume commun
    evite que le worker n'annonce l'absence d'OpenMVG alors que COLMAP est
    installe et parfaitement utilisable, ce qui inquiete pour rien.
    """
    if tools.has_openmvg and tools.has_openmvs:
        version = "2.x" if tools.modern_openmvg else "1.x"
        return "complet", [
            f"Chaine complete : OpenMVG {version} + OpenMVS.",
            "Tous les profils sont disponibles, jusqu'au maillage texture.",
        ]

    if tools.colmap_dense:
        return "complet", [
            f"COLMAP avec CUDA detecte : {tools.get('colmap')}",
            "Tous les profils sont disponibles, jusqu'au maillage.",
            "La texture reste du ressort d'OpenMVS ; COLMAP colore par sommet.",
        ]

    if tools.has_colmap:
        lignes = [
            f"COLMAP detecte (sans CUDA) : {tools.get('colmap')}",
            "Profil « Nuage epars seulement » disponible.",
            "Pour un maillage : la version « -cuda » de COLMAP si la machine a",
            "une carte NVIDIA, sinon OpenMVG et OpenMVS.",
        ]
        return "partiel", lignes

    if not tools.missing_for("openmvg", sparse_only=True):
        return "partiel", [
            "OpenMVG detecte sans OpenMVS.",
            "Profil « Nuage epars seulement » disponible ; pas de maillage.",
        ]

    return "absent", [
        "Aucune chaine de reconstruction detectee.",
        "Seul le televersement des photos fonctionne pour l'instant.",
        "Le plus simple : installer COLMAP (voir README), puis renseigner",
        "PHOTOGRAM_COLMAP_BIN dans .env si l'executable n'est pas dans le PATH.",
    ]


def preset_availability(tools: Toolchain, preference: str = "auto") -> Dict[str, dict]:
    """Pour chaque profil, dit s'il peut tourner ici et pourquoi non le cas echeant.

    Permet a l'interface de decourager un profil voue a l'echec avant de
    mobiliser le worker, plutot que de le laisser echouer apres coup.
    """
    etat = {}
    for cle, preset in PRESETS.items():
        backend = tools.choose_backend(preference, preset.sparse_only)
        manquants = tools.missing_for(backend, preset.sparse_only)

        if backend == "colmap" and not preset.sparse_only and not tools.colmap_dense:
            etat[cle] = {
                "disponible": False,
                "backend": backend,
                "motif": (
                    "Cette version de COLMAP est compilee sans CUDA et s'arrete "
                    "au nuage epars. Installez la version « -cuda » de COLMAP, "
                    "ou OpenMVG et OpenMVS."
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
    "resume_chaine",
]
