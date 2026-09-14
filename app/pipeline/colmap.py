"""Chaine COLMAP.

Interet : COLMAP existe en paquet Debian (``apt install colmap``) et en
binaires Windows officiels, la ou OpenMVG et OpenMVS demandent une
compilation de plusieurs heures.

La densification (``patch_match_stereo``) exige CUDA, sans equivalent CPU. Sur
une machine equipee d'une carte NVIDIA et avec la version « -cuda », la chaine
va donc jusqu'au maillage sans rien compiler ; sinon elle s'arrete au nuage
epars, et le maillage demande OpenMVG + OpenMVS.

COLMAP ne texture pas les maillages : il produit un maillage colore par
sommet, ce qui suffit a visualiser la forme. Une vraie texture reste du
ressort d'OpenMVS.

**Les noms d'options changent d'une version a l'autre** : ``SiftExtraction``
est devenu ``FeatureExtraction`` dans les versions recentes, par exemple.
Plutot que de coder en dur un jeu de noms qui sera faux ailleurs, chaque
commande demande a COLMAP la liste de ses options (``--help``) et n'emploie
que celles qui existent reellement. Une option introuvable est simplement
omise : COLMAP applique alors sa valeur par defaut, ce qui reste correct.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set

from .plan import PlanContext, Step, collect_artifacts, prepare_images

#: Au-dela, la mise en correspondance exhaustive devient couteuse ; on bascule
#: sur un appariement sequentiel, adapte a une prise de vue en rotation.
SEUIL_APPARIEMENT_EXHAUSTIF = 60

#: Ce que COLMAP affiche quand l'acceleration graphique de SIFT se derobe.
MOTIFS_GPU = ("gpu", "opengl", "siftgpu", "glew", "display")

#: Options acceptees, par (binaire, taille, date, sous-commande). Interroger
#: COLMAP coute un lancement de processus ; le plan est construit a chaque job.
_CACHE_OPTIONS: Dict[tuple, Set[str]] = {}

_MOTIF_OPTION = re.compile(r"--([A-Za-z][A-Za-z0-9_.]*)")


def options_disponibles(colmap: str, sous_commande: str) -> Set[str]:
    """Options acceptees par une sous-commande, lues dans son aide.

    Un ensemble vide signifie « inconnu » : l'aide n'a pas pu etre obtenue, et
    l'appelant se rabat alors sur les noms historiques.
    """
    try:
        etat = os.stat(colmap)
        cle = (colmap, etat.st_size, int(etat.st_mtime), sous_commande)
    except OSError:
        return set()

    if cle in _CACHE_OPTIONS:
        return _CACHE_OPTIONS[cle]

    options: Set[str] = set()
    extra = {}
    if os.name == "nt":
        extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        execution = subprocess.run(
            [colmap, sous_commande, "--help"],
            capture_output=True, text=True, errors="replace", timeout=60, **extra,
        )
        for nom in _MOTIF_OPTION.findall(execution.stdout + execution.stderr):
            options.add("--" + nom)
    except (OSError, subprocess.SubprocessError, ValueError):
        options = set()

    _CACHE_OPTIONS[cle] = options
    return options


class Commande:
    """Construit une ligne de commande en n'employant que des options connues."""

    def __init__(self, ctx: PlanContext, sous_commande: str):
        self.ctx = ctx
        self.sous_commande = sous_commande
        self.colmap = ctx.tools.require("colmap")
        self.disponibles = options_disponibles(self.colmap, sous_commande)
        self.argv: List[str] = [self.colmap, sous_commande]

    def obligatoire(self, nom: str, valeur) -> "Commande":
        """Option sans laquelle la commande n'a pas de sens : toujours passee."""
        self.argv += [nom, str(valeur)]
        return self

    def dossier_sortie(self, nom: str, chemin) -> "Commande":
        """Option designant un dossier que COLMAP n'ouvrira pas s'il manque.

        Plusieurs commandes, dont ``mapper``, refusent de creer leur dossier
        de sortie et s'arretent sur « output_path is not a directory ». Le
        declarer ainsi rend la creation impossible a oublier.
        """
        Path(chemin).mkdir(parents=True, exist_ok=True)
        return self.obligatoire(nom, chemin)

    def fichier_sortie(self, nom: str, chemin) -> "Commande":
        """Option designant un fichier : c'est son dossier parent qui doit exister."""
        Path(chemin).parent.mkdir(parents=True, exist_ok=True)
        return self.obligatoire(nom, chemin)

    def facultative(self, alias: Sequence[str], valeur, suffixe: str = "") -> "Commande":
        """Option de reglage, passee seulement si cette version la connait."""
        nom = self.resoudre(alias, suffixe)
        if nom:
            self.argv += [nom, str(valeur)]
        return self

    def resoudre(self, alias: Sequence[str], suffixe: str = "") -> Optional[str]:
        if not self.disponibles:
            # Aide indisponible : on tente le nom historique plutot que rien.
            return alias[0] if alias else None
        for nom in alias:
            if nom in self.disponibles:
                return nom
        if suffixe:
            # Dernier recours : une option dont le nom se termine par le
            # reglage cherche, a condition qu'elle soit sans ambiguite.
            candidats = sorted(
                nom for nom in self.disponibles
                if nom.endswith("." + suffixe) or nom == "--" + suffixe
            )
            if len(candidats) == 1:
                return candidats[0]
        return None

    def build(self) -> List[str]:
        return list(self.argv)


# --------------------------------------------------------------------------
# Chemins et reglages derives
# --------------------------------------------------------------------------


def _database(ctx: PlanContext):
    return ctx.mvg_dir / "colmap.db"


def _sparse_dir(ctx: PlanContext):
    return ctx.mvg_dir / "sparse"


def _taille_max(ctx: PlanContext) -> int:
    """Plafond de resolution pour les etapes denses.

    COLMAP attend -1 pour « aucune limite », la ou les profils expriment
    l'absence de plafond par 0.
    """
    return ctx.preset.densify_max_resolution or -1


def _gpu(ctx: PlanContext) -> str:
    """1 si COLMAP doit utiliser le GPU pour SIFT, 0 sinon.

    L'extraction et l'appariement sur GPU sont d'un ordre de grandeur plus
    rapides, mais cette acceleration depend d'un contexte graphique qui n'est
    pas toujours disponible : PHOTOGRAM_COLMAP_GPU permet de l'ecarter sans
    renoncer a la densification, qui n'a rien a voir.
    """
    from ..config import settings

    reglage = settings.colmap_gpu.lower()
    if reglage in ("1", "true", "oui", "on"):
        return "1"
    if reglage in ("0", "false", "non", "off"):
        return "0"
    return "1" if ctx.tools.colmap_dense else "0"


# --------------------------------------------------------------------------
# Commandes
# --------------------------------------------------------------------------

#: Le prefixe des options SIFT a change de nom selon les versions ; on accepte
#: les deux, et le suffixe sert de filet si un troisieme nom apparait.
ALIAS_EXTRACTION = {
    "use_gpu": ("--SiftExtraction.use_gpu", "--FeatureExtraction.use_gpu"),
    "num_threads": ("--SiftExtraction.num_threads", "--FeatureExtraction.num_threads"),
    "max_image_size": ("--SiftExtraction.max_image_size", "--FeatureExtraction.max_image_size"),
}

ALIAS_APPARIEMENT = {
    "use_gpu": ("--SiftMatching.use_gpu", "--FeatureMatching.use_gpu"),
    "num_threads": ("--SiftMatching.num_threads", "--FeatureMatching.num_threads"),
}


def _extraction_argv(ctx: PlanContext) -> list:
    commande = Commande(ctx, "feature_extractor")
    commande.fichier_sortie("--database_path", _database(ctx))
    commande.obligatoire("--image_path", ctx.images_dir)
    # Une seule camera pour toute la serie : c'est le cas courant (un seul
    # appareil) et cela stabilise nettement la calibration sur peu de vues.
    commande.facultative(("--ImageReader.single_camera",), "1")
    commande.facultative(("--ImageReader.camera_model",), "SIMPLE_RADIAL")
    commande.facultative(ALIAS_EXTRACTION["use_gpu"], _gpu(ctx), "use_gpu")
    commande.facultative(ALIAS_EXTRACTION["num_threads"], ctx.threads, "num_threads")
    commande.facultative(ALIAS_EXTRACTION["max_image_size"], ctx.preset_max_image_size)
    return commande.build()


def _appariement_argv(ctx: PlanContext) -> list:
    exhaustif = len(ctx.source_photos) <= SEUIL_APPARIEMENT_EXHAUSTIF
    commande = Commande(ctx, "exhaustive_matcher" if exhaustif else "sequential_matcher")
    commande.fichier_sortie("--database_path", _database(ctx))
    commande.facultative(ALIAS_APPARIEMENT["use_gpu"], _gpu(ctx), "use_gpu")
    commande.facultative(ALIAS_APPARIEMENT["num_threads"], ctx.threads, "num_threads")
    return commande.build()


def _mapper_argv(ctx: PlanContext) -> list:
    commande = Commande(ctx, "mapper")
    commande.obligatoire("--database_path", _database(ctx))
    commande.obligatoire("--image_path", ctx.images_dir)
    commande.dossier_sortie("--output_path", _sparse_dir(ctx))
    commande.facultative(("--Mapper.num_threads",), ctx.threads, "num_threads")
    return commande.build()


def _export_argv(ctx: PlanContext) -> list:
    commande = Commande(ctx, "model_converter")
    commande.obligatoire("--input_path", ctx.colmap_model)
    commande.fichier_sortie("--output_path", ctx.out_dir / "nuage_epars.ply")
    commande.obligatoire("--output_type", "PLY")
    return commande.build()


def _rapport_argv(ctx: PlanContext) -> list:
    # Export texte du modele : lisible tel quel, et reutilisable par d'autres
    # outils (Blender, Meshroom) si l'utilisateur veut poursuivre ailleurs.
    commande = Commande(ctx, "model_converter")
    commande.obligatoire("--input_path", ctx.colmap_model)
    commande.dossier_sortie("--output_path", ctx.out_dir)
    commande.obligatoire("--output_type", "TXT")
    return commande.build()


def _undistort_argv(ctx: PlanContext) -> list:
    # Le calcul dense suppose des images sans distorsion ; COLMAP les reecrit
    # dans un espace de travail dedie, avec les cameras associees.
    commande = Commande(ctx, "image_undistorter")
    commande.obligatoire("--image_path", ctx.images_dir)
    commande.obligatoire("--input_path", ctx.colmap_model)
    commande.dossier_sortie("--output_path", ctx.mvs_dir)
    commande.facultative(("--output_type",), "COLMAP")
    commande.facultative(("--max_image_size",), _taille_max(ctx))
    return commande.build()


def _stereo_argv(ctx: PlanContext) -> list:
    commande = Commande(ctx, "patch_match_stereo")
    commande.obligatoire("--workspace_path", ctx.mvs_dir)
    commande.facultative(("--workspace_format",), "COLMAP")
    # La verification de coherence geometrique double le temps de calcul mais
    # elimine l'essentiel du bruit ; sans elle, le nuage dense est trop sale
    # pour donner un maillage exploitable.
    commande.facultative(("--PatchMatchStereo.geom_consistency",), "true")
    commande.facultative(("--PatchMatchStereo.max_image_size",), _taille_max(ctx))
    return commande.build()


def _fusion_argv(ctx: PlanContext) -> list:
    commande = Commande(ctx, "stereo_fusion")
    commande.obligatoire("--workspace_path", ctx.mvs_dir)
    commande.fichier_sortie("--output_path", ctx.out_dir / "nuage_dense.ply")
    commande.facultative(("--workspace_format",), "COLMAP")
    commande.facultative(("--input_type",), "geometric")
    return commande.build()


def _maillage_argv(ctx: PlanContext) -> list:
    commande = Commande(ctx, "poisson_mesher")
    commande.obligatoire("--input_path", ctx.out_dir / "nuage_dense.ply")
    commande.fichier_sortie("--output_path", ctx.out_dir / "maillage.ply")
    return commande.build()


def _choisir_modele(ctx: PlanContext, log: Callable[[str], None]) -> None:
    """Retient le plus gros modele produit par le mapper.

    COLMAP ecrit ``sparse/0``, ``sparse/1``, ... : un modele par groupe
    d'images qu'il a su relier entre elles. Plusieurs dossiers signalent une
    serie fragmentee, ce qui merite d'etre dit a l'utilisateur.
    """
    racine = _sparse_dir(ctx)
    modeles = sorted(p for p in racine.glob("*") if p.is_dir()) if racine.is_dir() else []
    if not modeles:
        raise RuntimeError(
            "COLMAP n'a reconstruit aucun modele. Causes habituelles : "
            "recouvrement insuffisant entre les photos (visez 60-80 %), sujet "
            "sans texture, ou photos floues."
        )

    def poids(modele):
        fichier = modele / "images.bin"
        return fichier.stat().st_size if fichier.is_file() else 0

    meilleur = max(modeles, key=poids)
    if len(modeles) > 1:
        log(
            f"ATTENTION : {len(modeles)} modeles distincts reconstruits, la serie "
            f"est fragmentee. Seul le plus complet ({meilleur.name}) est conserve. "
            "Ajoutez des photos de transition entre les zones."
        )
    ctx.colmap_model = meilleur
    log(f"Modele retenu : {meilleur.relative_to(ctx.job_dir)}")


# --------------------------------------------------------------------------
# Repli
# --------------------------------------------------------------------------


def _verifier_nuage(ctx: PlanContext, log: Callable[[str], None]) -> None:
    """Refuse de continuer sur un nuage epars vide.

    Les etapes denses acceptent sans broncher un nuage sans point et rendent
    des fichiers vides apres des heures de calcul : mieux vaut s'arreter ici,
    ou la cause est encore identifiable.
    """
    from . import ply

    nuage = ctx.out_dir / "nuage_epars.ply"
    if not nuage.is_file():
        raise RuntimeError("L'export du nuage epars n'a produit aucun fichier.")

    if ply.est_vide(nuage):
        raise RuntimeError(
            "Le nuage epars ne contient aucun point. Les cameras ont ete "
            "positionnees mais aucune structure n'a ete reconstruite : "
            "recouvrement insuffisant entre les photos (visez 60-80 %), sujet "
            "sans texture, ou photos floues."
        )

    log(f"Nuage epars : {ply.resume(nuage)}.")


def _repli_sans_gpu(construire, alias: Sequence[str]):
    """Rejoue la meme etape sur processeur si le GPU s'est derobe.

    Un echec d'acceleration graphique est frequent et sans rapport avec les
    donnees : perdre une reconstruction pour cela serait absurde, alors que la
    meme etape aboutit sur processeur, seulement plus lentement.
    """

    def repli(ctx: PlanContext, sortie: str):
        if _gpu(ctx) != "1":
            return None  # deja sur processeur, rien a tenter
        if not any(motif in sortie for motif in MOTIFS_GPU):
            return None

        argv = [str(part) for part in construire(ctx)]
        # Le nom retenu depend de la version : on cherche celui qui a
        # effectivement ete employe plutot que de le supposer.
        for nom in alias:
            if nom in argv:
                argv[argv.index(nom) + 1] = "0"
                return argv
        for index, part in enumerate(argv):
            if part.endswith("use_gpu"):
                argv[index + 1] = "0"
                return argv
        return None  # cette version ne permet pas de choisir : rien a retenter

    return repli


def build_colmap_plan(ctx: PlanContext) -> List[Step]:
    etapes = [
        Step("Preparation des images", func=prepare_images),
        Step("Detection des points caracteristiques", argv=_extraction_argv,
             repli=_repli_sans_gpu(_extraction_argv, ALIAS_EXTRACTION["use_gpu"])),
        Step("Mise en correspondance", argv=_appariement_argv,
             repli=_repli_sans_gpu(_appariement_argv, ALIAS_APPARIEMENT["use_gpu"])),
        Step("Positionnement des cameras (SfM)", argv=_mapper_argv),
        Step("Controle de la reconstruction", func=_choisir_modele),
        Step("Export du nuage colore", argv=_export_argv),
        Step("Controle du nuage epars", func=_verifier_nuage),
        Step("Export du modele en texte", argv=_rapport_argv, optional=True),
    ]

    if not ctx.preset.sparse_only:
        etapes += [
            Step("Correction de la distorsion", argv=_undistort_argv),
            Step("Calcul des cartes de profondeur (GPU)", argv=_stereo_argv),
            Step("Fusion du nuage dense", argv=_fusion_argv),
            # Le maillage peut echouer sur un nuage trop clairseme sans que
            # cela invalide le nuage dense, qui reste exploitable.
            Step("Reconstruction du maillage", argv=_maillage_argv, optional=True),
        ]

    etapes.append(Step("Collecte des resultats", func=collect_artifacts))
    return etapes
