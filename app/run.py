"""Lancement local : interface web et worker en une seule commande.

    python -m app.run

Pense pour une machine de travail : ni droits root, ni systemd, ni
installation dans /opt. Le depot se suffit a lui-meme, les donnees vont dans
./data, et tout s'arrete avec Ctrl+C.

Pour un serveur permanent qui redemarre tout seul, preferer les unites
systemd de deploy/ (voir README).
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent


def _memoire_totale_mo() -> int:
    """Memoire vive de la machine en Mio, 0 si illisible.

    Passe par app.hardware, qui n'importe pas la configuration : a cet instant
    le .env n'existe peut-etre pas encore.
    """
    from . import hardware

    return hardware.memoire()["total"] // (1024 * 1024)


def reglages_pour_cette_machine() -> dict:
    """Valeurs initiales deduites du materiel.

    Les defauts du projet sont calibres pour 1 Go de RAM ; les appliquer tels
    quels a un ordinateur de bureau limiterait les photos a 1600 px et le lot a
    40 images sans aucune raison.
    """
    ram_mo = _memoire_totale_mo()
    coeurs = os.cpu_count() or 2
    threads = max(1, coeurs - 1)

    if ram_mo >= 16000:
        return {"work_max_dim": 3200, "max_photos": 300, "threads": threads, "profil": "high"}
    if ram_mo >= 8000:
        return {"work_max_dim": 2400, "max_photos": 150, "threads": threads, "profil": "balanced"}
    if ram_mo >= 4000:
        return {"work_max_dim": 2000, "max_photos": 80, "threads": threads, "profil": "rpi"}
    return {"work_max_dim": 1600, "max_photos": 40, "threads": min(3, threads), "profil": "sparse"}


def creer_env_si_absent(chemin: Path) -> str:
    """Ecrit un .env adapte a la machine. Renvoie le mot de passe s'il est neuf."""
    if chemin.is_file():
        return ""

    modele = (RACINE / ".env.example").read_text(encoding="utf-8")
    reglages = reglages_pour_cette_machine()
    mot_de_passe = secrets.token_urlsafe(9)

    remplacements = {
        "PHOTOGRAM_PASSWORD": mot_de_passe,
        "PHOTOGRAM_SECRET_KEY": secrets.token_hex(32),
        "PHOTOGRAM_DATA_DIR": str(RACINE / "data"),
        "PHOTOGRAM_WORK_MAX_DIM": str(reglages["work_max_dim"]),
        "PHOTOGRAM_MAX_PHOTOS": str(reglages["max_photos"]),
        "PHOTOGRAM_THREADS": str(reglages["threads"]),
    }

    lignes = []
    for ligne in modele.splitlines():
        cle = ligne.split("=", 1)[0] if "=" in ligne else ""
        lignes.append(f"{cle}={remplacements[cle]}" if cle in remplacements else ligne)
    chemin.write_text("\n".join(lignes) + "\n", encoding="utf-8")

    try:
        chemin.chmod(0o600)  # sans effet notable sur Windows, inoffensif
    except OSError:
        pass

    print(f"  Configuration creee    : {chemin}")
    print(f"  Reglages deduits       : {_memoire_totale_mo() or '?'} Mio de RAM, "
          f"{os.cpu_count()} coeurs -> {reglages['work_max_dim']} px, "
          f"{reglages['threads']} threads")
    return mot_de_passe


def adresses_locales(port: int) -> list:
    adresses = [f"http://127.0.0.1:{port}"]
    try:
        # Aucune donnee n'est emise : cette connexion UDP sert seulement a
        # demander au systeme quelle interface il utiliserait pour sortir.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as prise:
            prise.connect(("192.0.2.1", 80))
            locale = prise.getsockname()[0]
        if not locale.startswith("127."):
            adresses.append(f"http://{locale}:{port}")
    except OSError:
        pass
    return adresses


#: Reglages dont une valeur vide est frequemment a l'origine d'un blocage.
CLES_SURVEILLEES = (
    "PHOTOGRAM_COLMAP_BIN",
    "PHOTOGRAM_OPENMVG_BIN",
    "PHOTOGRAM_OPENMVS_BIN",
)


def _etat_du_env(chemin: Path) -> None:
    """Detaille ce que le fichier .env contient reellement.

    Afficher « (non renseigne) » sans rien de plus laisse l'utilisateur sans
    prise : il ne sait pas s'il a edite le mauvais fichier, si son edition
    n'a pas ete enregistree, ou si la cle est absente du fichier.
    """
    from .config import lire_texte_tolerant

    print(f"  Fichier      : {chemin}")
    if not chemin.is_file():
        print("    [KO] Ce fichier n'existe pas ; il sera cree au prochain lancement.")
        return

    contenu = lire_texte_tolerant(chemin)
    lignes = contenu.splitlines()
    print(f"    {len(lignes)} lignes, {chemin.stat().st_size} octets")

    # Piege classique sous Windows : le Bloc-notes ajoute .txt a un fichier
    # sans extension, et l'edition part dans un fichier que rien ne lit.
    for parasite in sorted(chemin.parent.glob(".env.*")):
        if parasite.name != ".env.example":
            print(f"    [KO] Fichier parasite : {parasite.name}")
            print("         Votre edition a probablement atterri la. Renommez-le en .env")

    for cle in CLES_SURVEILLEES:
        trouvees = [
            ligne.strip() for ligne in lignes
            if ligne.strip().startswith(cle + "=")
        ]
        if not trouvees:
            print(f"    [--] {cle} : absent du fichier")
            continue
        for ligne in trouvees:
            renseignee = ligne.split("=", 1)[1].strip() != ""
            print(f"    {'[ok]' if renseignee else '[--]'} {ligne}"
                  + ("" if renseignee else "   (vide)"))
        if len(trouvees) > 1:
            print(f"         {len(trouvees)} lignes pour cette cle : la derniere gagne.")


def definir_reglage(chemin: Path, expression: str) -> int:
    """Ecrit CLE=VALEUR dans le .env, sans passer par un editeur de texte.

    Editer le fichier a la main sous Windows accumule les pieges : le
    Bloc-notes qui ajoute une extension .txt, une ligne ajoutee a cote de
    celle du modele, un chemin colle avec des guillemets. Autant proposer une
    commande qui ne peut pas se tromper.
    """
    from .config import lire_texte_tolerant

    if "=" not in expression:
        print(f"  Attendu CLE=VALEUR, recu : {expression}", file=sys.stderr)
        return 1

    cle, _, valeur = expression.partition("=")
    cle, valeur = cle.strip(), valeur.strip().strip('"').strip("'")

    if not cle.startswith("PHOTOGRAM_"):
        print(f"  « {cle} » n'est pas un reglage PhotoGram (prefixe PHOTOGRAM_ attendu).",
              file=sys.stderr)
        return 1

    if not chemin.is_file():
        creer_env_si_absent(chemin)

    # La premiere occurrence est mise a jour et les suivantes supprimees : le
    # fichier garde une seule ligne par reglage, lisible sans se demander
    # laquelle fait foi.
    resultat = []
    trouvee = False
    for ligne in lire_texte_tolerant(chemin).splitlines():
        if ligne.strip().startswith(cle + "="):
            if trouvee:
                continue  # doublon herite d'une edition precedente
            resultat.append(f"{cle}={valeur}")
            trouvee = True
        else:
            resultat.append(ligne)
    if not trouvee:
        resultat.append(f"{cle}={valeur}")
    lignes = resultat

    chemin.write_text("\n".join(lignes) + "\n", encoding="utf-8")

    print(f"  {cle} = {valeur}")
    print(f"  Ecrit dans {chemin}" + ("" if trouvee else "   (cle ajoutee)"))

    if cle.endswith("_BIN") and valeur and not Path(valeur).is_dir():
        print()
        print(f"  ATTENTION : le dossier {valeur} n'existe pas.")
        print("  Verifiez le chemin : il doit designer le dossier CONTENANT")
        print("  l'executable (souvent le sous-dossier « bin » de l'archive).")
        return 1

    print()
    print("  Verifiez la detection avec :  --diagnostic")
    return 0


def diagnostic() -> int:
    """Etat de l'installation, sur n'importe quel systeme.

    L'equivalent de scripts/diagnose.sh, qui est un script bash et ne sert donc
    a rien sous Windows. Ne divulgue ni mot de passe ni cle : la sortie peut
    etre recopiee telle quelle dans un ticket.
    """
    import platform

    from . import hardware
    from .config import settings
    from .pipeline import detect_toolchain, resume_chaine

    print()
    print("=== Machine ===")
    print(f"  Systeme      : {platform.platform()}")
    print(f"  Python       : {platform.python_version()} ({sys.executable})")
    memoire = hardware.memoire()
    total = memoire['total'] / (1024 ** 3)
    print(f"  Processeur   : {os.cpu_count()} coeurs")
    print(f"  Memoire      : {total:.1f} Gio"
          + ("" if memoire["complet"] else " (detail indisponible sur ce systeme)"))

    print()
    print("=== Configuration ===")
    _etat_du_env(RACINE / ".env")
    print(f"  Donnees      : {settings.data_dir}"
          + ("" if settings.data_dir.is_dir() else "   [absent, sera cree]"))
    print(f"  Ecoute       : {settings.host}:{settings.port}")
    print(f"  Threads      : {settings.threads}")
    print(f"  Resolution   : {settings.work_max_dim} px    Photos max : {settings.max_photos}")
    print(f"  Backend      : {settings.backend}")
    for probleme in settings.problems():
        print(f"  [KO] {probleme}")

    print()
    print("=== Chaine de reconstruction ===")
    outils = detect_toolchain()
    niveau, lignes = resume_chaine(outils)
    for index, ligne in enumerate(lignes):
        # Le marqueur ne porte que sur le constat : les lignes suivantes sont
        # des explications, les prefixer toutes noierait le signal.
        marqueur = "[KO] " if (niveau == "absent" and index == 0) else "     "
        print(f"  {marqueur}{ligne}")

    if outils.binaries:
        print()
        print("  Executables trouves :")
        for nom, chemin in sorted(outils.binaries.items()):
            print(f"    {nom:<40} {chemin}")
    else:
        print()
        print("  Dossiers explores en plus du PATH :")
        for reglage, valeur in (
            ("PHOTOGRAM_COLMAP_BIN", settings.colmap_bin),
            ("PHOTOGRAM_OPENMVG_BIN", settings.openmvg_bin),
            ("PHOTOGRAM_OPENMVS_BIN", settings.openmvs_bin),
        ):
            etat = valeur or "(non renseigne)"
            if valeur and not Path(valeur).is_dir():
                etat += "   [ce dossier n'existe pas]"
            print(f"    {reglage:<24} {etat}")
        print()
        print("  Pour renseigner un dossier sans editer le fichier a la main :")
        print("    python -m app.run --definir PHOTOGRAM_COLMAP_BIN=C:\\chemin\\vers\\bin")

    print()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.run",
        description="Lance PhotoGram (interface web + worker) sur cette machine.",
    )
    parser.add_argument("--host", default=None, help="interface d'ecoute (defaut : .env)")
    parser.add_argument("--port", type=int, default=None, help="port d'ecoute (defaut : .env)")
    parser.add_argument("--sans-worker", action="store_true",
                        help="ne lance que l'interface web (worker deja demarre ailleurs)")
    parser.add_argument("--dev", action="store_true",
                        help="rechargement automatique du code (developpement)")
    parser.add_argument("--diagnostic", action="store_true",
                        help="affiche l'etat de l'installation et quitte")
    parser.add_argument("--definir", metavar="CLE=VALEUR",
                        help="ecrit un reglage dans .env puis quitte "
                             "(ex: --definir PHOTOGRAM_COLMAP_BIN=C:\\colmap\\bin)")
    args = parser.parse_args(argv)

    print("PhotoGram")
    # Le .env doit exister avant l'import de la configuration, qui la fige.
    mot_de_passe = creer_env_si_absent(RACINE / ".env")

    if args.definir:
        return definir_reglage(RACINE / ".env", args.definir)

    if args.diagnostic:
        return diagnostic()

    from .config import settings

    settings.ensure_dirs()
    hote = args.host or settings.host
    port = args.port or settings.port

    from .pipeline import detect_toolchain

    outils = detect_toolchain()
    if outils.backends():
        print(f"  Chaine detectee        : {', '.join(outils.backends())}")
    else:
        print("  Chaine detectee        : aucune — seul le televersement fonctionnera.")
        print("                           Voir README, section « chaine de reconstruction ».")

    worker = None
    if not args.sans_worker:
        # Le worker surveille ce PID : si ce lanceur disparait brutalement
        # (terminal ferme, processus tue), il s'arrete au lieu de survivre en
        # orphelin avec la base ouverte.
        env = dict(os.environ, PHOTOGRAM_PARENT_PID=str(os.getpid()))
        worker = subprocess.Popen(
            [sys.executable, "-m", "app.worker"], cwd=str(RACINE), env=env
        )
        print(f"  Worker demarre         : PID {worker.pid}")

    print(f"  Donnees                : {settings.data_dir}")
    for adresse in adresses_locales(port):
        print(f"  Interface              : {adresse}")
    if mot_de_passe:
        print(f"  Mot de passe           : {mot_de_passe}")
    else:
        # Le mot de passe n'est affiche en clair qu'a sa creation ; ensuite on
        # se contente de rappeler ou le lire, sans quoi on le cherche en vain.
        print(f"  Mot de passe           : ligne PHOTOGRAM_PASSWORD de {RACINE / '.env'}")
    print("  Ctrl+C pour arreter.")
    print()

    import uvicorn

    try:
        uvicorn.run("app.main:app", host=hote, port=port, reload=args.dev, log_level="info")
    except KeyboardInterrupt:
        pass
    except OSError as erreur:
        print(f"\nImpossible d'ecouter sur {hote}:{port} — {erreur}", file=sys.stderr)
        print("Un autre programme occupe deja ce port ? Essayez --port 8001.", file=sys.stderr)
        return 1
    finally:
        if worker is not None:
            worker.terminate()
            try:
                worker.wait(timeout=20)
            except subprocess.TimeoutExpired:
                worker.kill()
            print("Worker arrete.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
