"""Lecture de l'en-tete d'un fichier PLY.

Sert a verifier qu'un resultat contient quelque chose : une commande peut
reussir, renvoyer 0 et n'ecrire qu'un en-tete sans le moindre sommet. Sans ce
controle, l'echec ne se decouvre qu'a l'ouverture de la visionneuse, une fois
le calcul termine.

L'en-tete d'un PLY est toujours en ASCII, meme quand les donnees qui suivent
sont binaires : on peut donc le lire sans decoder le fichier entier.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

#: Un en-tete depasse rarement quelques dizaines de lignes ; au-dela, le
#: fichier n'est pas un PLY et il est inutile de le parcourir.
LIGNES_MAX = 200


def elements(chemin: Path) -> Dict[str, int]:
    """Nombre d'elements declares, par nom : {"vertex": 12345, "face": 0}.

    Renvoie un dictionnaire vide si le fichier n'est pas un PLY lisible.
    """
    compte: Dict[str, int] = {}
    try:
        with open(chemin, "rb") as handle:
            premiere = handle.readline().strip()
            if premiere[:3] != b"ply":
                return {}
            for _ in range(LIGNES_MAX):
                ligne = handle.readline()
                if not ligne:
                    break
                texte = ligne.decode("ascii", "replace").strip()
                if texte == "end_header":
                    break
                mots = texte.split()
                if len(mots) >= 3 and mots[0] == "element":
                    try:
                        compte[mots[1]] = int(mots[2])
                    except ValueError:
                        continue
    except OSError:
        return {}
    return compte


def resume(chemin: Path) -> str:
    """Description courte pour le journal : « 12 345 sommets, 24 000 faces »."""
    declares = elements(chemin)
    if not declares:
        return "format non reconnu"

    morceaux = []
    for nom, libelle in (("vertex", "sommets"), ("face", "faces")):
        if nom in declares:
            morceaux.append(f"{declares[nom]:n} {libelle}")
    return ", ".join(morceaux) if morceaux else "aucun element declare"


def est_vide(chemin: Path) -> bool:
    """Vrai si le fichier ne declare aucun sommet : rien a afficher."""
    declares = elements(chemin)
    return bool(declares) and declares.get("vertex", 0) == 0
