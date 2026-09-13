"""Traitement d'images a l'ingestion : EXIF, vignettes, copies de travail, nettete.

Toutes les operations sont ecrites pour un RPi3 : on s'appuie sur le mode
``draft`` de Pillow, qui laisse le decodeur JPEG reduire l'image pendant la
decompression.  Ouvrir une photo de 12 Mpx coute alors quelques Mo au lieu de
plusieurs dizaines, et c'est plusieurs fois plus rapide.

Pillow est la seule dependance native du projet, et volontairement : chaque
paquet compile de plus est un paquet susceptible de n'avoir aucune roue
precompilee pour l'architecture du Pi, donc de se recompiler sur place.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from PIL import Image, ImageFilter, ImageOps, ImageStat
from PIL.ExifTags import TAGS

Image.MAX_IMAGE_PIXELS = 80_000_000  # garde-fou anti decompression bomb

THUMB_SIZE = 360
SHARPNESS_SIZE = 900

#: En dessous de ce score de variance du laplacien, la photo est floue.
#: Valeur empirique : a affiner selon l'appareil, d'ou l'affichage du score
#: brut dans l'interface plutot qu'un simple verdict binaire.
SHARPNESS_FLOOR = 60.0


def _exif_dict(img: Image.Image) -> dict:
    try:
        raw = img.getexif()
    except Exception:
        return {}
    if not raw:
        return {}
    out = {}
    for tag_id, value in raw.items():
        out[TAGS.get(tag_id, tag_id)] = value
    # Les champs Exif detailles (dont FocalLength) vivent dans l'IFD dedie.
    try:
        for tag_id, value in raw.get_ifd(0x8769).items():
            out[TAGS.get(tag_id, tag_id)] = value
    except Exception:
        pass
    return out


def _as_float(value) -> Optional[float]:
    """Convertit une valeur EXIF en float.

    Selon la version de Pillow et l'IFD lu, un rationnel arrive soit comme
    ``IFDRational`` (convertible directement), soit comme un couple brut
    ``(numerateur, denominateur)``.  Les deux formes sont acceptees.
    """
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            return None
        try:
            numerator, denominator = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
        if denominator == 0:
            return None
        value = numerator / denominator
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num) or num <= 0:
        return None
    return num


def read_meta(path: Path) -> dict:
    """Dimensions + metadonnees EXIF utiles a la photogrammetrie."""
    meta = {
        "width": 0,
        "height": 0,
        "camera": "",
        "focal_mm": None,
        "taken_at": "",
    }
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img) or img
        meta["width"], meta["height"] = img.size
        exif = _exif_dict(img)

    make = str(exif.get("Make", "") or "").strip()
    model = str(exif.get("Model", "") or "").strip()
    if model.lower().startswith(make.lower()) and make:
        camera = model
    else:
        camera = " ".join(p for p in (make, model) if p)
    meta["camera"] = camera

    meta["focal_mm"] = _as_float(exif.get("FocalLength"))
    taken = exif.get("DateTimeOriginal") or exif.get("DateTime") or ""
    meta["taken_at"] = str(taken).strip()
    return meta


#: Laplacien 4-connexe. Le decalage de 128 recentre les valeurs signees dans
#: l'intervalle 0-255 que Pillow sait representer : les fortes transitions
#: saturent, ce qui comprime le haut de l'echelle sans gener le classement.
_NOYAU_LAPLACIEN = ImageFilter.Kernel(
    (3, 3), [0, -1, 0, -1, 4, -1, 0, -1, 0], scale=1, offset=128
)


def sharpness_score(path: Path) -> Optional[float]:
    """Variance du laplacien : plus c'est haut, plus l'image est nette.

    Calcule avec Pillow seul. Une version numpy serait un peu plus precise sur
    les images tres floues, mais elle imposerait une dependance native de plus
    a installer sur le Pi pour un gain nul : dans la zone qui nous interesse,
    celle de la decision nette/flou, les deux mesures se valent.
    """
    try:
        with Image.open(path) as img:
            img.draft("L", (SHARPNESS_SIZE, SHARPNESS_SIZE))
            img = ImageOps.exif_transpose(img) or img
            img = img.convert("L")
            img.thumbnail((SHARPNESS_SIZE, SHARPNESS_SIZE), Image.BILINEAR)
            if min(img.size) < 3:
                return None
            laplacien = img.filter(_NOYAU_LAPLACIEN)
            return float(ImageStat.Stat(laplacien).var[0])
    except Exception:
        return None


def make_thumbnail(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        img.draft("RGB", (THUMB_SIZE, THUMB_SIZE))
        img = ImageOps.exif_transpose(img) or img
        img = img.convert("RGB")
        img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        img.save(dst, "JPEG", quality=80, optimize=True)


def make_work_copy(src: Path, dst: Path, max_dim: int) -> tuple:
    """Copie reduite destinee au pipeline. Renvoie (largeur, hauteur).

    L'EXIF est **conserve volontairement** : OpenMVG en tire la focale et le
    modele d'appareil pour initialiser la calibration.  Le redimensionnement
    ne pose pas de probleme puisque la focale en pixels est recalculee a
    partir de la largeur reelle du fichier fourni.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        img.draft("RGB", (max_dim, max_dim))
        img = ImageOps.exif_transpose(img) or img
        # L'EXIF est relu *apres* la rotation : exif_transpose neutralise la
        # balise Orientation, sans quoi les outils du pipeline pivoteraient
        # une seconde fois une image deja redressee.
        exif_bytes = img.info.get("exif")
        img = img.convert("RGB")
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        params = {"quality": 92, "subsampling": 0}
        if exif_bytes:
            params["exif"] = exif_bytes
        img.save(dst, "JPEG", **params)
        return img.size
