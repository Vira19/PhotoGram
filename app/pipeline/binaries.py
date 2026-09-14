"""Detection des binaires OpenMVG / OpenMVS.

Les paquets et les compilations maison n'installent pas les binaires au meme
endroit, et OpenMVG a renomme plusieurs executables entre la 1.x et la 2.x.
On sonde donc le systeme une fois, et le plan d'execution s'adapte a ce qui a
reellement ete trouve, plutot que de supposer une version.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings

#: Emplacements frequents, en plus du PATH.
EXTRA_DIRS = [
    "/usr/local/bin/openMVG",
    "/usr/local/bin/OpenMVS",
    "/usr/local/bin",
    "/opt/openMVG/bin",
    "/opt/openMVS/bin",
    "/usr/bin",
]

SENSOR_DB_CANDIDATES = [
    "/usr/local/share/openMVG/sensor_width_camera_database.txt",
    "/usr/share/openMVG/sensor_width_camera_database.txt",
    "/usr/local/bin/sensor_width_camera_database.txt",
    "/opt/openMVG/share/openMVG/sensor_width_camera_database.txt",
]

#: Binaires OpenMVG requis quelle que soit la version.
MVG_REQUIRED = [
    "openMVG_main_SfMInit_ImageListing",
    "openMVG_main_ComputeFeatures",
    "openMVG_main_ComputeMatches",
    "openMVG_main_ComputeSfM_DataColor",
]

#: Presents seulement en OpenMVG >= 2.0.
MVG_MODERN = [
    "openMVG_main_PairGenerator",
    "openMVG_main_GeometricFilter",
    "openMVG_main_SfM",
]

#: Nom du moteur SfM en OpenMVG 1.x.
MVG_LEGACY_SFM = "openMVG_main_IncrementalSfM"

MVG_TO_MVS = "openMVG_main_openMVG2openMVS"

#: COLMAP : un seul binaire a sous-commandes, disponible en paquet Debian.
COLMAP_BINARY = "colmap"

#: Binaires OpenMVS. Certaines distributions les prefixent.
MVS_BINARIES = ["DensifyPointCloud", "ReconstructMesh", "RefineMesh", "TextureMesh"]
MVS_PREFIXES = ["", "OpenMVS_", "openMVS_"]


def _search_dirs() -> List[Path]:
    dirs = []
    for raw in (settings.openmvg_bin, settings.openmvs_bin, settings.colmap_bin):
        if raw:
            dirs.append(Path(raw))
    dirs.extend(Path(d) for d in EXTRA_DIRS)
    return [d for d in dirs if d.is_dir()]


def _find(name: str, prefixes: Optional[List[str]] = None) -> Optional[str]:
    """Cherche un executable dans le PATH puis dans les dossiers configures.

    La recherche passe entierement par ``shutil.which``, y compris pour les
    dossiers explicites : c'est lui qui applique PATHEXT sous Windows, ou le
    fichier s'appelle ``colmap.exe`` et non ``colmap``. Une comparaison de nom
    exacte y rendrait PHOTOGRAM_COLMAP_BIN et consorts sans effet.
    """
    dossiers = os.pathsep.join(str(d) for d in _search_dirs())
    for prefix in prefixes or [""]:
        candidate = prefix + name
        found = shutil.which(candidate)
        if found:
            return found
        if dossiers:
            found = shutil.which(candidate, path=dossiers)
            if found:
                return found
    return None


@dataclass
class Toolchain:
    """Instantane de ce qui est installe sur la machine."""

    binaries: Dict[str, str] = field(default_factory=dict)
    sensor_db: Optional[str] = None
    modern_openmvg: bool = False

    def get(self, name: str) -> Optional[str]:
        return self.binaries.get(name)

    def require(self, name: str) -> str:
        path = self.binaries.get(name)
        if not path:
            raise FileNotFoundError(f"Binaire introuvable : {name}")
        return path

    @property
    def has_openmvg(self) -> bool:
        needed = MVG_REQUIRED + [MVG_TO_MVS]
        if not all(n in self.binaries for n in needed):
            return False
        return self.modern_openmvg or MVG_LEGACY_SFM in self.binaries

    @property
    def has_colmap(self) -> bool:
        return COLMAP_BINARY in self.binaries

    @property
    def has_openmvs(self) -> bool:
        # RefineMesh est facultatif : il n'est jamais utilise sur un RPi.
        return all(n in self.binaries for n in ("DensifyPointCloud", "ReconstructMesh", "TextureMesh"))

    def backends(self) -> List[str]:
        """Chaines utilisables, de la plus complete a la plus legere."""
        disponibles = []
        if self.has_openmvg:
            disponibles.append("openmvg")
        if self.has_colmap:
            disponibles.append("colmap")
        return disponibles

    def missing(self) -> List[str]:
        """Ce qui manque pour la chaine complete OpenMVG + OpenMVS."""
        return self.missing_for("openmvg", sparse_only=False)

    def missing_for(self, backend: str, sparse_only: bool) -> List[str]:
        """Binaires manquants pour un backend et un niveau de finition donnes.

        Un profil « nuage epars » n'a pas besoin d'OpenMVS : le signaler comme
        manquant empecherait de travailler sur un RPi ou seul le SfM est
        installe.
        """
        if backend == "colmap":
            return [] if self.has_colmap else [COLMAP_BINARY]

        out = [name for name in MVG_REQUIRED if name not in self.binaries]
        if not self.modern_openmvg and MVG_LEGACY_SFM not in self.binaries:
            out.append("openMVG_main_SfM (ou openMVG_main_IncrementalSfM)")
        if not sparse_only:
            if MVG_TO_MVS not in self.binaries:
                out.append(MVG_TO_MVS)
            out += [
                name for name in ("DensifyPointCloud", "ReconstructMesh", "TextureMesh")
                if name not in self.binaries
            ]
        return out

    def choose_backend(self, preference: str, sparse_only: bool) -> str:
        """Backend retenu pour un job.

        En mode automatique, OpenMVG est privilegie des qu'il est complet pour
        le travail demande : c'est la seule chaine qui va jusqu'au maillage.
        COLMAP prend le relais pour le nuage epars, car il s'installe d'un
        simple apt la ou OpenMVG demande une compilation de plusieurs heures.
        """
        if preference in ("openmvg", "colmap"):
            return preference
        if not self.missing_for("openmvg", sparse_only):
            return "openmvg"
        if self.has_colmap:
            # Retenu meme pour un profil que COLMAP ne sait pas honorer : le
            # worker expliquera la limite, message bien plus utile qu'une
            # longue liste de binaires OpenMVG absents.
            return "colmap"
        return "openmvg"

    def summary(self) -> dict:
        return {
            "openmvg": self.has_openmvg,
            "openmvs": self.has_openmvs,
            "colmap": self.has_colmap,
            "backends": self.backends(),
            "version_openmvg": "2.x" if self.modern_openmvg else "1.x",
            "sensor_db": self.sensor_db,
            "manquants": self.missing(),
            "trouves": dict(sorted(self.binaries.items())),
        }


def detect_toolchain() -> Toolchain:
    tools = Toolchain()

    for name in MVG_REQUIRED + MVG_MODERN + [MVG_LEGACY_SFM, MVG_TO_MVS]:
        path = _find(name)
        if path:
            tools.binaries[name] = path

    tools.modern_openmvg = all(n in tools.binaries for n in MVG_MODERN)

    colmap = _find(COLMAP_BINARY)
    if colmap:
        tools.binaries[COLMAP_BINARY] = colmap

    for name in MVS_BINARIES:
        path = _find(name, MVS_PREFIXES)
        if path:
            tools.binaries[name] = path

    if settings.sensor_db and Path(settings.sensor_db).is_file():
        tools.sensor_db = settings.sensor_db
    else:
        for candidate in SENSOR_DB_CANDIDATES:
            if Path(candidate).is_file():
                tools.sensor_db = candidate
                break

    return tools
