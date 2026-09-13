"""Profils de qualite.

Un Raspberry Pi 3 dispose de 1 Go de RAM partagee avec le GPU et n'a pas de
CUDA : la densification est de tres loin l'etape la plus couteuse.  Les profils
jouent donc surtout sur deux leviers : la resolution a laquelle travaille
OpenMVS, et le fait de densifier ou non.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str

    #: Qualite du detecteur SIFT : NORMAL, HIGH ou ULTRA.
    feature_preset: str = "NORMAL"
    #: Methode de mise en correspondance OpenMVG.
    matching_method: str = "ANNL2"
    #: S'arreter apres le nuage epars (pas d'OpenMVS du tout).
    sparse_only: bool = False
    #: Sous-echantillonnage OpenMVS : 0 = pleine resolution, 3 = divise par 8.
    densify_resolution_level: int = 3
    #: Plafond dur sur le cote long pendant la densification (px).
    densify_max_resolution: int = 640
    #: Nombre de vues fusionnees par point.
    densify_number_views: int = 3
    #: Passe de raffinement du maillage (tres gourmande).
    refine_mesh: bool = False
    #: Sous-echantillonnage pour la texture.
    texture_resolution_level: int = 2
    #: Avertissement affiche dans l'interface.
    warning: str = ""


PRESETS: Dict[str, Preset] = {
    "sparse": Preset(
        key="sparse",
        label="Nuage epars seulement",
        description=(
            "Detection, mise en correspondance et positionnement des cameras, "
            "sans densification. Produit un nuage de points colore. "
            "C'est le seul profil vraiment confortable sur un RPi3 : comptez "
            "10 a 40 min pour 20 photos."
        ),
        feature_preset="NORMAL",
        sparse_only=True,
    ),
    "rpi": Preset(
        key="rpi",
        label="Raspberry Pi (maillage minimal)",
        description=(
            "Chaine complete jusqu'au maillage texture, a resolution fortement "
            "reduite. Prevoyez plusieurs heures pour 20 photos et surveillez "
            "la memoire : sans swap, la densification peut etre tuee par l'OOM."
        ),
        feature_preset="NORMAL",
        densify_resolution_level=3,
        densify_max_resolution=640,
        densify_number_views=3,
        refine_mesh=False,
        texture_resolution_level=2,
        warning=(
            "Profil limite : sur RPi3, activez au moins 2 Go de swap avant de "
            "lancer une densification."
        ),
    ),
    "balanced": Preset(
        key="balanced",
        label="Equilibre (machine de bureau)",
        description=(
            "Bon compromis qualite/temps sur une machine disposant de 8 Go de "
            "RAM ou plus. A eviter sur RPi3."
        ),
        feature_preset="HIGH",
        matching_method="AUTO",
        densify_resolution_level=1,
        densify_max_resolution=1600,
        densify_number_views=4,
        refine_mesh=False,
        texture_resolution_level=1,
        warning="Demande au moins 8 Go de RAM.",
    ),
    "high": Preset(
        key="high",
        label="Qualite maximale",
        description=(
            "Pleine resolution, avec raffinement du maillage. Reserve a la "
            "future machine : 16 Go de RAM et beaucoup de patience."
        ),
        feature_preset="ULTRA",
        matching_method="AUTO",
        densify_resolution_level=0,
        densify_max_resolution=0,  # 0 = pas de plafond
        densify_number_views=5,
        refine_mesh=True,
        texture_resolution_level=0,
        warning="Demande au moins 16 Go de RAM.",
    ),
}

DEFAULT_PRESET = "sparse"


def get_preset(key: str) -> Preset:
    return PRESETS.get(key, PRESETS[DEFAULT_PRESET])
