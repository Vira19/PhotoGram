"""Chaine COLMAP : nuage epars, sans GPU.

Interet sur un Raspberry Pi : COLMAP existe en paquet Debian
(``apt install colmap``), la ou OpenMVG et OpenMVS demandent une compilation
de plusieurs heures sur la machine cible.

La densification (``patch_match_stereo``) exige en revanche CUDA, sans
equivalent CPU. Sur une machine equipee d'une carte NVIDIA et avec la version
« -cuda » de COLMAP, la chaine va donc jusqu'au maillage sans rien compiler ;
sinon elle s'arrete au nuage epars, et le maillage demande OpenMVG + OpenMVS.

COLMAP ne texture pas les maillages : il produit un maillage colore par
sommet, ce qui suffit a visualiser la forme. Une vraie texture reste du
ressort d'OpenMVS.
"""

from __future__ import annotations

from typing import Callable, List

from .plan import PlanContext, Step, collect_artifacts, prepare_images

#: Au-dela, la mise en correspondance exhaustive devient couteuse ; on bascule
#: sur un appariement sequentiel, adapte a une prise de vue en rotation.
SEUIL_APPARIEMENT_EXHAUSTIF = 60


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
    """1 si COLMAP peut utiliser le GPU pour SIFT, 0 sinon.

    L'extraction et l'appariement sur GPU sont d'un ordre de grandeur plus
    rapides ; la version sans CUDA doit s'en passer.
    """
    return "1" if ctx.tools.colmap_dense else "0"


def _extraction_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("colmap"), "feature_extractor",
        "--database_path", _database(ctx),
        "--image_path", ctx.images_dir,
        # Une seule camera pour toute la serie : c'est le cas courant (un seul
        # appareil) et cela stabilise nettement la calibration sur peu de vues.
        "--ImageReader.single_camera", "1",
        "--ImageReader.camera_model", "SIMPLE_RADIAL",
        "--SiftExtraction.use_gpu", _gpu(ctx),
        "--SiftExtraction.num_threads", ctx.threads,
        "--SiftExtraction.max_image_size", ctx.preset_max_image_size,
    ]


def _appariement_argv(ctx: PlanContext) -> list:
    exhaustif = len(ctx.source_photos) <= SEUIL_APPARIEMENT_EXHAUSTIF
    commande = "exhaustive_matcher" if exhaustif else "sequential_matcher"
    return [
        ctx.tools.require("colmap"), commande,
        "--database_path", _database(ctx),
        "--SiftMatching.use_gpu", _gpu(ctx),
        "--SiftMatching.num_threads", ctx.threads,
    ]


def _mapper_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("colmap"), "mapper",
        "--database_path", _database(ctx),
        "--image_path", ctx.images_dir,
        "--output_path", _sparse_dir(ctx),
        "--Mapper.num_threads", ctx.threads,
    ]


def _choisir_modele(ctx: PlanContext, log: Callable[[str], None]) -> None:
    """Retient le plus gros modele produit par le mapper.

    COLMAP ecrit ``sparse/0``, ``sparse/1``, … : un modele par groupe d'images
    qu'il a su relier entre elles. Plusieurs dossiers signalent une serie
    fragmentee, ce qui merite d'etre dit a l'utilisateur.
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


def _export_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("colmap"), "model_converter",
        "--input_path", ctx.colmap_model,
        "--output_path", ctx.out_dir / "nuage_epars.ply",
        "--output_type", "PLY",
    ]


def _rapport_argv(ctx: PlanContext) -> list:
    # Export texte du modele : lisible tel quel, et reutilisable par d'autres
    # outils (Blender, Meshroom) si l'utilisateur veut poursuivre ailleurs.
    return [
        ctx.tools.require("colmap"), "model_converter",
        "--input_path", ctx.colmap_model,
        "--output_path", ctx.out_dir,
        "--output_type", "TXT",
    ]


def _undistort_argv(ctx: PlanContext) -> list:
    # Le calcul dense suppose des images sans distorsion ; COLMAP les
    # reecrit dans un espace de travail dedie, avec les cameras associees.
    return [
        ctx.tools.require("colmap"), "image_undistorter",
        "--image_path", ctx.images_dir,
        "--input_path", ctx.colmap_model,
        "--output_path", ctx.mvs_dir,
        "--output_type", "COLMAP",
        "--max_image_size", _taille_max(ctx),
    ]


def _stereo_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("colmap"), "patch_match_stereo",
        "--workspace_path", ctx.mvs_dir,
        "--workspace_format", "COLMAP",
        # La verification de coherence geometrique double le temps de calcul
        # mais elimine l'essentiel du bruit ; sans elle, le nuage dense est
        # trop sale pour donner un maillage exploitable.
        "--PatchMatchStereo.geom_consistency", "true",
        "--PatchMatchStereo.max_image_size", _taille_max(ctx),
    ]


def _fusion_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("colmap"), "stereo_fusion",
        "--workspace_path", ctx.mvs_dir,
        "--workspace_format", "COLMAP",
        "--input_type", "geometric",
        "--output_path", ctx.out_dir / "nuage_dense.ply",
    ]


def _maillage_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("colmap"), "poisson_mesher",
        "--input_path", ctx.out_dir / "nuage_dense.ply",
        "--output_path", ctx.out_dir / "maillage.ply",
    ]


def build_colmap_plan(ctx: PlanContext) -> List[Step]:
    etapes = [
        Step("Preparation des images", func=prepare_images),
        Step("Detection des points caracteristiques", argv=_extraction_argv),
        Step("Mise en correspondance", argv=_appariement_argv),
        Step("Positionnement des cameras (SfM)", argv=_mapper_argv),
        Step("Controle de la reconstruction", func=_choisir_modele),
        Step("Export du nuage colore", argv=_export_argv),
        Step("Export du modele en texte", argv=_rapport_argv, optional=True),
    ]

    if not ctx.preset.sparse_only:
        etapes += [
            Step("Correction de la distorsion", argv=_undistort_argv),
            Step("Calcul des cartes de profondeur (GPU)", argv=_stereo_argv),
            Step("Fusion du nuage dense", argv=_fusion_argv),
            # Le maillage peut echouer sur un nuage trop clairsemé sans que
            # cela invalide le nuage dense, qui reste exploitable.
            Step("Reconstruction du maillage", argv=_maillage_argv, optional=True),
        ]

    etapes.append(Step("Collecte des resultats", func=collect_artifacts))
    return etapes
