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
    args = parser.parse_args(argv)

    print("PhotoGram")
    # Le .env doit exister avant l'import de la configuration, qui la fige.
    mot_de_passe = creer_env_si_absent(RACINE / ".env")

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
