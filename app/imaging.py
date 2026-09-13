"""Traitement d'images a l'ingestion : EXIF, vignettes, copies de travail, nettete.

Toutes les operations sont ecrites pour un RPi3 : on s'appuie sur le mode
``draft`` de Pillow, qui laisse le decodeur JPEG reduire l'image pendant la
decompression.  Ouvrir une photo de 12 Mpx coute alors quelques Mo au lieu de
plusieurs dizaines, et c'est plusieurs fois plus rapide.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageOps
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


def sharpness_score(path: Path) -> Optional[float]:
    """Variance du laplacien : plus c'est haut, plus l'image est nette.

    Le laplacien est calcule a la main par decalages numpy ; cela evite de
    tirer OpenCV ou SciPy, qui pesent lourd a installer sur un RPi3.
    """
    try:
        with Image.open(path) as img:
            img.draft("L", (SHARPNESS_SIZE, SHARPNESS_SIZE))
            img = ImageOps.exif_transpose(img) or img
            img = img.convert("L")
            img.thumbnail((SHARPNESS_SIZE, SHARPNESS_SIZE), Image.BILINEAR)
            arr = np.asarray(img, dtype=np.float32)
    except Exception:
        return None

    if arr.ndim != 2 or min(arr.shape) < 3:
        return None

    lap = (
        4.0 * arr[1:-1, 1:-1]
        - arr[:-2, 1:-1]
        - arr[2:, 1:-1]
        - arr[1:-1, :-2]
        - arr[1:-1, 2:]
    )
    return float(lap.var())


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
