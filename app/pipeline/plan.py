"""Construction du plan d'execution d'une reconstruction.

Le plan est une simple liste d'etapes ordonnees. Chaque etape est soit une
commande externe, soit une fonction Python (preparation, verifications,
collecte des resultats).  Les arguments d'une commande peuvent etre produits
paresseusement : certaines valeurs, comme la focale de repli, ne sont connues
qu'une fois les images preparees.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import ply
from .binaries import MVG_LEGACY_SFM, Toolchain
from .presets import Preset

#: Extensions recuperees comme resultats exploitables.
ARTIFACT_EXTENSIONS = {".ply", ".obj", ".mtl", ".png", ".jpg"}


@dataclass
class PlanContext:
    """Tout ce dont les etapes ont besoin pour s'executer."""

    job_id: int
    project_id: int
    job_dir: Path
    preset: Preset
    tools: Toolchain
    threads: int
    #: Copies de travail des photos retenues (chemins absolus).
    source_photos: List[Path] = field(default_factory=list)
    #: Focale de repli en pixels, calculee a la preparation si l'EXIF est muet.
    focal_px: Optional[float] = None
    #: Chaine retenue pour ce job : "openmvg" ou "colmap".
    backend: str = "openmvg"
    #: Sous-modele COLMAP conserve, renseigne en cours d'execution.
    colmap_model: Optional[Path] = None

    @property
    def preset_max_image_size(self) -> int:
        """Plafond de resolution passe aux detecteurs de points.

        Les copies de travail sont deja reduites ; ce plafond sert de second
        garde-fou pour les profils les plus legers.
        """
        if self.preset.densify_max_resolution:
            return max(800, self.preset.densify_max_resolution * 2)
        return 3200

    @property
    def images_dir(self) -> Path:
        return self.job_dir / "images"

    @property
    def mvg_dir(self) -> Path:
        return self.job_dir / "mvg"

    @property
    def matches_dir(self) -> Path:
        return self.mvg_dir / "matches"

    @property
    def recon_dir(self) -> Path:
        return self.mvg_dir / "reconstruction"

    @property
    def mvs_dir(self) -> Path:
        return self.job_dir / "mvs"

    @property
    def out_dir(self) -> Path:
        return self.job_dir / "out"

    def make_dirs(self) -> None:
        for path in (self.images_dir, self.matches_dir, self.recon_dir, self.mvs_dir, self.out_dir):
            path.mkdir(parents=True, exist_ok=True)


@dataclass
class Step:
    """Une etape du plan.

    ``optional`` marque les etapes dont l'echec n'invalide pas la
    reconstruction : perdre la texture laisse un maillage exploitable, il
    serait absurde de jeter plusieurs heures de calcul pour autant.
    """

    name: str
    argv: Optional[Callable] = None      # callable(ctx) -> list
    func: Optional[Callable] = None      # callable(ctx, log) -> None
    cwd: Optional[Callable] = None       # callable(ctx) -> Path
    optional: bool = False
    #: callable(ctx, sortie) -> list | None : commande de seconde chance,
    #: choisie d'apres ce que l'outil a affiche en echouant.
    repli: Optional[Callable] = None

    def resolve_argv(self, ctx: PlanContext) -> List[str]:
        if self.argv is None:
            return []
        return [str(part) for part in self.argv(ctx)]


# --------------------------------------------------------------------------
# Etapes Python
# --------------------------------------------------------------------------


def prepare_images(ctx: PlanContext, log: Callable[[str], None]) -> None:
    """Rassemble les photos retenues dans un dossier dedie au job.

    On tente un lien physique avant de copier : sur la carte SD d'un RPi,
    dupliquer 40 images de plusieurs Mo a chaque reconstruction use le support
    pour rien.
    """
    ctx.make_dirs()
    if not ctx.source_photos:
        raise RuntimeError("Aucune photo retenue pour cette reconstruction.")

    linked = copied = 0
    for source in ctx.source_photos:
        target = ctx.images_dir / source.name
        if target.exists():
            continue
        try:
            target.hardlink_to(source)
            linked += 1
        except (OSError, AttributeError):
            shutil.copy2(source, target)
            copied += 1

    log(f"{len(ctx.source_photos)} photos preparees ({linked} liees, {copied} copiees).")

    if ctx.focal_px:
        log(f"Aucune focale EXIF exploitable : repli sur {ctx.focal_px:.0f} px.")
    else:
        log("Focale deduite de l'EXIF et de la base de capteurs OpenMVG.")


def check_sfm(ctx: PlanContext, log: Callable[[str], None]) -> None:
    """Verifie que le SfM a bien produit une reconstruction.

    OpenMVG sort parfois avec un code 0 tout en n'ayant enregistre aucune
    camera (recouvrement insuffisant). Sans ce controle, le job continuerait
    pendant des heures sur des donnees vides.
    """
    sfm_data = ctx.recon_dir / "sfm_data.bin"
    if not sfm_data.is_file() or sfm_data.stat().st_size == 0:
        raise RuntimeError(
            "Le SfM n'a produit aucune reconstruction. Causes habituelles : "
            "recouvrement insuffisant entre les photos (visez 60-80 %), sujet "
            "trop uniforme ou sans texture, ou photos floues."
        )
    log(f"Reconstruction epars ecrite ({sfm_data.stat().st_size // 1024} Kio).")


def collect_artifacts(ctx: PlanContext, log: Callable[[str], None]) -> None:
    """Rapatrie dans ``out/`` tout ce qui est exploitable.

    Les noms de sortie d'OpenMVS varient d'une version a l'autre : on ramasse
    par extension plutot que de deviner des noms de fichiers.
    """
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    found = 0
    for source in sorted(ctx.mvs_dir.iterdir()) if ctx.mvs_dir.is_dir() else []:
        if not source.is_file() or source.suffix.lower() not in ARTIFACT_EXTENSIONS:
            continue
        target = ctx.out_dir / source.name
        if target.exists():
            continue
        shutil.copy2(source, target)
        found += 1

    existing = sorted(p for p in ctx.out_dir.glob("*") if p.is_file())
    if not existing:
        raise RuntimeError("Le pipeline s'est termine sans produire aucun fichier.")

    log(f"{found} fichiers rapatries. Resultats :")
    vides = []
    for chemin in existing:
        detail = f"{chemin.stat().st_size // 1024} Kio"
        if chemin.suffix.lower() == ".ply":
            # Une commande peut reussir et n'ecrire qu'un en-tete : le dire
            # ici evite de le decouvrir en ouvrant la visionneuse.
            detail += f", {ply.resume(chemin)}"
            if ply.est_vide(chemin):
                vides.append(chemin.name)
                detail += "   ← VIDE"
        log(f"  {chemin.name} : {detail}")

    if vides:
        log("")
        log(
            "ATTENTION : " + ", ".join(vides) + " ne contien" + ("nent" if len(vides) > 1 else "t")
            + " aucun point. La reconstruction a abouti mais n'a rien trouve a "
            "reconstruire : recouvrement insuffisant entre les photos, sujet sans "
            "texture, ou photos floues."
        )


# --------------------------------------------------------------------------
# Construction du plan
# --------------------------------------------------------------------------


def _listing_argv(ctx: PlanContext) -> list:
    argv = [
        ctx.tools.require("openMVG_main_SfMInit_ImageListing"),
        "-i", ctx.images_dir,
        "-o", ctx.matches_dir,
        "-c", "3",  # modele PINHOLE_CAMERA_RADIAL3
    ]
    if ctx.tools.sensor_db:
        argv += ["-d", ctx.tools.sensor_db]
    if ctx.focal_px:
        argv += ["-f", f"{ctx.focal_px:.2f}"]
    return argv


def _features_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("openMVG_main_ComputeFeatures"),
        "-i", ctx.matches_dir / "sfm_data.json",
        "-o", ctx.matches_dir,
        "-m", "SIFT",
        "-p", ctx.preset.feature_preset,
        "-n", ctx.threads,
    ]


def _pairs_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("openMVG_main_PairGenerator"),
        "-i", ctx.matches_dir / "sfm_data.json",
        "-o", ctx.matches_dir / "pairs.bin",
    ]


def _matches_modern_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("openMVG_main_ComputeMatches"),
        "-i", ctx.matches_dir / "sfm_data.json",
        "-p", ctx.matches_dir / "pairs.bin",
        "-o", ctx.matches_dir / "matches.putative.bin",
        "-n", ctx.preset.matching_method,
    ]


def _filter_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("openMVG_main_GeometricFilter"),
        "-i", ctx.matches_dir / "sfm_data.json",
        "-m", ctx.matches_dir / "matches.putative.bin",
        "-o", ctx.matches_dir / "matches.f.bin",
        "-g", "f",
    ]


def _matches_legacy_argv(ctx: PlanContext) -> list:
    # En OpenMVG 1.x, ComputeMatches enchaine mise en correspondance et
    # filtrage geometrique, et nomme sa sortie matches.f.bin tout seul.
    return [
        ctx.tools.require("openMVG_main_ComputeMatches"),
        "-i", ctx.matches_dir / "sfm_data.json",
        "-o", ctx.matches_dir,
        "-g", "f",
    ]


def _sfm_argv(ctx: PlanContext) -> list:
    if ctx.tools.modern_openmvg:
        return [
            ctx.tools.require("openMVG_main_SfM"),
            "--sfm_engine", "INCREMENTAL",
            "--input_file", ctx.matches_dir / "sfm_data.json",
            "--match_dir", ctx.matches_dir,
            "--output_dir", ctx.recon_dir,
        ]
    return [
        ctx.tools.require(MVG_LEGACY_SFM),
        "-i", ctx.matches_dir / "sfm_data.json",
        "-m", ctx.matches_dir,
        "-o", ctx.recon_dir,
    ]


def _color_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("openMVG_main_ComputeSfM_DataColor"),
        "-i", ctx.recon_dir / "sfm_data.bin",
        "-o", ctx.out_dir / "nuage_epars.ply",
    ]


def _to_mvs_argv(ctx: PlanContext) -> list:
    # Le dossier des images redressees est cree ici : tous les outils ne
    # creent pas leur dossier de sortie, et le decouvrir en cours de
    # reconstruction coute une reprise complete.
    undistorted = ctx.mvs_dir / "undistorted"
    undistorted.mkdir(parents=True, exist_ok=True)
    return [
        ctx.tools.require("openMVG_main_openMVG2openMVS"),
        "-i", ctx.recon_dir / "sfm_data.bin",
        "-o", ctx.mvs_dir / "scene.mvs",
        "-d", undistorted,
    ]


def _densify_argv(ctx: PlanContext) -> list:
    argv = [
        ctx.tools.require("DensifyPointCloud"),
        "scene.mvs",
        "-o", "dense.mvs",
        "-w", ctx.mvs_dir,
        "--resolution-level", ctx.preset.densify_resolution_level,
        "--number-views", ctx.preset.densify_number_views,
        "--max-threads", ctx.threads,
    ]
    if ctx.preset.densify_max_resolution:
        argv += ["--max-resolution", ctx.preset.densify_max_resolution]
    return argv


def _mesh_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("ReconstructMesh"),
        "dense.mvs",
        "-o", "mesh.mvs",
        "-w", ctx.mvs_dir,
        "--max-threads", ctx.threads,
    ]


def _refine_argv(ctx: PlanContext) -> list:
    return [
        ctx.tools.require("RefineMesh"),
        "mesh.mvs",
        "-o", "mesh_refined.mvs",
        "-w", ctx.mvs_dir,
        "--resolution-level", 1,
        "--max-threads", ctx.threads,
    ]


def _texture_argv(ctx: PlanContext) -> list:
    source = "mesh_refined.mvs" if ctx.preset.refine_mesh else "mesh.mvs"
    return [
        ctx.tools.require("TextureMesh"),
        source,
        "-o", "texture.mvs",
        "-w", ctx.mvs_dir,
        "--resolution-level", ctx.preset.texture_resolution_level,
        "--export-type", "obj",
        "--max-threads", ctx.threads,
    ]


def build_plan(ctx: PlanContext) -> List[Step]:
    """Plan correspondant au backend retenu pour ce job."""
    if ctx.backend == "colmap":
        from .colmap import build_colmap_plan

        return build_colmap_plan(ctx)
    return build_openmvg_plan(ctx)


def build_openmvg_plan(ctx: PlanContext) -> List[Step]:
    steps: List[Step] = [
        Step("Preparation des images", func=prepare_images),
        Step("Inventaire et calibration initiale", argv=_listing_argv),
        Step("Detection des points caracteristiques", argv=_features_argv),
    ]

    if ctx.tools.modern_openmvg:
        steps += [
            Step("Generation des paires d'images", argv=_pairs_argv),
            Step("Mise en correspondance", argv=_matches_modern_argv),
            Step("Filtrage geometrique", argv=_filter_argv),
        ]
    else:
        steps += [Step("Mise en correspondance et filtrage", argv=_matches_legacy_argv)]

    steps += [
        Step("Positionnement des cameras (SfM)", argv=_sfm_argv),
        Step("Controle de la reconstruction", func=check_sfm),
        Step("Colorisation du nuage epars", argv=_color_argv),
    ]

    if ctx.preset.sparse_only:
        steps.append(Step("Collecte des resultats", func=collect_artifacts))
        return steps

    steps += [
        Step("Export vers OpenMVS", argv=_to_mvs_argv),
        Step("Densification du nuage", argv=_densify_argv),
        Step("Reconstruction du maillage", argv=_mesh_argv),
    ]

    if ctx.preset.refine_mesh:
        steps.append(Step("Raffinement du maillage", argv=_refine_argv, optional=True))

    steps += [
        Step("Texturation", argv=_texture_argv, optional=True),
        Step("Collecte des resultats", func=collect_artifacts),
    ]
    return steps
