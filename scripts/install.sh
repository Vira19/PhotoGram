#!/usr/bin/env bash
# Installation de PhotoGram sur un Raspberry Pi (ou toute Debian/Ubuntu).
#
#   sudo ./scripts/install.sh
#
# Le script est idempotent : on peut le relancer pour mettre a jour.
# En cas de probleme : ./scripts/diagnose.sh

set -euo pipefail

RACINE=/opt/photogram
UTILISATEUR=photogram
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Signale l'etape exacte qui a echoue : un « set -e » muet oblige sinon a
# relire tout le script pour comprendre ce qui s'est passe.
ETAPE="demarrage"
echec() {
  echo >&2
  echo "ECHEC pendant : $ETAPE (ligne $1)" >&2
  echo >&2
  echo "Pour un diagnostic complet :  ./scripts/diagnose.sh" >&2
  exit 1
}
trap 'echec $LINENO' ERR

if [[ $EUID -ne 0 ]]; then
  echo "Ce script doit etre lance avec sudo." >&2
  exit 1
fi

# --- Verifications prealables --------------------------------------------

ETAPE="verification de la version de Python"
LISIBLE=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  cat >&2 <<ECHEC

    Python $LISIBLE detecte, or PhotoGram demande Python 3.9 ou plus recent.
    Raspberry Pi OS « Bullseye » fournit 3.9 et « Bookworm » 3.11 ; une version
    anterieure signifie un systeme trop ancien. Mettez-le a jour :

        sudo apt update && sudo apt full-upgrade

ECHEC
  exit 1
fi

ETAPE="verification de l'architecture"
ARCH=$(uname -m)
if [[ "$ARCH" == armv6l || "$ARCH" == armv7l ]]; then
  cat <<AVERTISSEMENT

    Architecture 32 bits detectee ($ARCH).

    Plusieurs dependances Python (pydantic-core notamment) n'ont pas de version
    precompilee pour cette architecture : pip tentera de les compiler, ce qui
    demande Rust et echoue le plus souvent sur un Pi.

    Si l'installation des dependances echoue plus bas, deux options :
      - reinstaller le Pi en Raspberry Pi OS 64 bits (recommande, un RPi3 le
        supporte : uname -m doit alors afficher aarch64) ;
      - ouvrir un ticket pour demander la variante sans FastAPI du projet.

AVERTISSEMENT
fi

ETAPE="verification du repertoire source"
if [[ "$SOURCE" == "$RACINE" ]]; then
  echo "Le depot est deja dans $RACINE : clonez-le ailleurs (par exemple" >&2
  echo "dans votre repertoire personnel) et relancez le script depuis la." >&2
  exit 1
fi

# --- Installation ---------------------------------------------------------

ETAPE="installation des paquets systeme"
apt-get update -qq
# Pillow est la seule dependance a compiler si aucune roue n'est disponible ;
# libjpeg et zlib lui sont alors indispensables.
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev build-essential libjpeg-dev zlib1g-dev

ETAPE="creation de l'utilisateur de service"
id -u "$UTILISATEUR" &>/dev/null \
  || useradd --system --home "$RACINE" --shell /usr/sbin/nologin "$UTILISATEUR"

ETAPE="copie du code dans $RACINE"
mkdir -p "$RACINE"
# Les repertoires de code sont vides d'abord : tar, contrairement a rsync,
# n'a pas d'equivalent de --delete, et un fichier supprime du depot resterait
# sinon indefiniment sur la machine.
rm -rf "$RACINE/app" "$RACINE/deploy" "$RACINE/scripts"
# tar plutot que rsync : rsync n'est pas installe par defaut sur Raspberry Pi
# OS Lite, alors que tar l'est toujours.
tar -C "$SOURCE" \
  --exclude='./.git' --exclude='./.venv' --exclude='./data' --exclude='./.env' \
  --exclude='__pycache__' --exclude='./tests' --exclude='*.pyc' \
  --exclude='./.pytest_cache' \
  -cf - . | tar -C "$RACINE" -xf -

ETAPE="lecture de la configuration existante"
if [[ -f "$RACINE/.env" ]]; then
  # shellcheck disable=SC1091
  DONNEES=$(grep -E '^PHOTOGRAM_DATA_DIR=' "$RACINE/.env" | cut -d= -f2- | tr -d '"'"'"' ')
fi
DONNEES=${DONNEES:-/var/lib/photogram}

ETAPE="creation de l'environnement Python"
if [[ ! -d "$RACINE/.venv" ]]; then
  python3 -m venv "$RACINE/.venv"
fi
"$RACINE/.venv/bin/pip" install --quiet --upgrade pip

ETAPE="installation des dependances Python"
# piwheels sert des roues precompilees pour ARM : sans lui, Pillow se
# recompile, ce qui prend une bonne demi-heure sur un RPi3.
if ! "$RACINE/.venv/bin/pip" install \
      --extra-index-url https://www.piwheels.org/simple \
      -r "$RACINE/requirements.txt"; then
  cat >&2 <<ECHEC

    L'installation des dependances Python a echoue.

    Causes frequentes :
      - architecture 32 bits ($ARCH) sans roue precompilee : voir l'avertissement
        plus haut ;
      - pas de reseau, ou piwheels injoignable : reessayez sans l'index
        supplementaire, la compilation sera longue mais devrait aboutir :
            $RACINE/.venv/bin/pip install -r $RACINE/requirements.txt
      - memoire insuffisante pendant une compilation : ajoutez du swap.

ECHEC
  exit 1
fi

ETAPE="ecriture de la configuration"
if [[ ! -f "$RACINE/.env" ]]; then
  cp "$RACINE/.env.example" "$RACINE/.env"
  MOT_DE_PASSE="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  CLE="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  sed -i "s|^PHOTOGRAM_PASSWORD=.*|PHOTOGRAM_PASSWORD=$MOT_DE_PASSE|" "$RACINE/.env"
  sed -i "s|^PHOTOGRAM_SECRET_KEY=.*|PHOTOGRAM_SECRET_KEY=$CLE|" "$RACINE/.env"
  sed -i "s|^PHOTOGRAM_DATA_DIR=.*|PHOTOGRAM_DATA_DIR=$DONNEES|" "$RACINE/.env"
  NOUVEAU_MOT_DE_PASSE=$MOT_DE_PASSE
else
  echo "    .env existant conserve."
fi
chmod 600 "$RACINE/.env"

ETAPE="preparation du repertoire de donnees ($DONNEES)"
mkdir -p "$DONNEES"
chown -R "$UTILISATEUR:$UTILISATEUR" "$DONNEES" "$RACINE"

ETAPE="installation des services systemd"
for unite in photogram-web photogram-worker; do
  # ProtectSystem=strict rend tout le disque en lecture seule : si les donnees
  # ne sont pas a l'emplacement par defaut (un disque USB, comme conseille),
  # le service ne pourrait rien y ecrire sans cette substitution.
  sed "s|^ReadWritePaths=.*|ReadWritePaths=$DONNEES|" \
    "$SOURCE/deploy/$unite.service" > "/etc/systemd/system/$unite.service"
done
systemctl daemon-reload
systemctl enable --quiet photogram-web photogram-worker
systemctl restart photogram-web photogram-worker

ETAPE="verification du demarrage des services"
sleep 3
for unite in photogram-web photogram-worker; do
  if ! systemctl is-active --quiet "$unite"; then
    echo >&2
    echo "Le service $unite n'a pas demarre. Journal :" >&2
    echo >&2
    journalctl -u "$unite" -n 25 --no-pager >&2
    exit 1
  fi
done

# --- Conseils post-installation ------------------------------------------

SWAP_MO=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
RAM_MO=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if (( RAM_MO < 1536 && SWAP_MO < 1024 )); then
  cat <<'AVERTISSEMENT'

    ATTENTION : moins de 1 Go de swap sur une machine a faible memoire.
    Une reconstruction sera tuee par l'OOM killer. Sur Raspberry Pi OS :

        sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
        sudo systemctl restart dphys-swapfile

AVERTISSEMENT
fi

IP=$(hostname -I | awk '{print $1}')
PORT=$(grep -E '^PHOTOGRAM_PORT=' "$RACINE/.env" | cut -d= -f2- || echo 8000)

echo
echo "Termine. Interface : http://${IP}:${PORT:-8000}"
if [[ -n "${NOUVEAU_MOT_DE_PASSE:-}" ]]; then
  echo "Mot de passe : $NOUVEAU_MOT_DE_PASSE   (modifiable dans $RACINE/.env)"
fi
echo
echo "Installez ensuite la chaine de reconstruction :"
echo "    sudo $SOURCE/scripts/install_pipeline.sh"
