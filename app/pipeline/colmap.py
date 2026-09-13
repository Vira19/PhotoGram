"""Chaine COLMAP : nuage epars, sans GPU.

Interet sur un Raspberry Pi : COLMAP existe en paquet Debian
(``apt install colmap``), la ou OpenMVG et OpenMVS demandent une compilation
de plusieurs heures sur la machine cible.

Limite assumee : la densification de COLMAP (``patch_match_stereo``) exige
CUDA et n'a pas d'equivalent CPU. Ce backend s'arrete donc au nuage epars ;
pour un maillage, il faut la chaine OpenMVG + OpenMVS.
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


def _extraction_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("colmap"), "feature_extractor",
        "--database_path", _database(ctx),
        "--image_path", ctx.images_dir,
        # Une seule camera pour toute la serie : c'est le cas courant (un seul
        # appareil) et cela stabilise nettement la calibration sur peu de vues.
        "--ImageReader.single_camera", "1",
        "--ImageReader.camera_model", "SIMPLE_RADIAL",
        "--SiftExtraction.use_gpu", "0",
        "--SiftExtraction.num_threads", ctx.threads,
        "--SiftExtraction.max_image_size", ctx.preset_max_image_size,
    ]


def _appariement_argv(ctx: PlanContext) -> list:
    exhaustif = len(ctx.source_photos) <= SEUIL_APPARIEMENT_EXHAUSTIF
    commande = "exhaustive_matcher" if exhaustif else "sequential_matcher"
    return [
        ctx.tools.require("colmap"), commande,
        "--database_path", _database(ctx),
        "--SiftMatching.use_gpu", "0",
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


def build_colmap_plan(ctx: PlanContext) -> List[Step]:
    return [
        Step("Preparation des images", func=prepare_images),
        Step("Detection des points caracteristiques", argv=_extraction_argv),
        Step("Mise en correspondance", argv=_appariement_argv),
        Step("Positionnement des cameras (SfM)", argv=_mapper_argv),
        Step("Controle de la reconstruction", func=_choisir_modele),
        Step("Export du nuage colore", argv=_export_argv),
        Step("Export du modele en texte", argv=_rapport_argv, optional=True),
        Step("Collecte des resultats", func=collect_artifacts),
    ]
