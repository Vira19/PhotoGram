#!/usr/bin/env bash
# Installation de PhotoGram sur un Raspberry Pi (ou toute Debian/Ubuntu).
#
#   sudo ./scripts/install.sh
#
# Le script est idempotent : on peut le relancer pour mettre a jour.

set -euo pipefail

RACINE=/opt/photogram
DONNEES=/var/lib/photogram
UTILISATEUR=photogram
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Ce script doit etre lance avec sudo." >&2
  exit 1
fi

echo "==> Paquets systeme"
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev build-essential libjpeg-dev zlib1g-dev

echo "==> Utilisateur de service"
id -u "$UTILISATEUR" &>/dev/null || useradd --system --home "$RACINE" --shell /usr/sbin/nologin "$UTILISATEUR"

echo "==> Copie du code dans $RACINE"
mkdir -p "$RACINE"
# --delete garde la cible propre, mais ni la config ni le venv ne doivent
# disparaitre lors d'une mise a jour.
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'data' --exclude '.env' \
  --exclude '__pycache__' --exclude 'tests' \
  "$SOURCE/" "$RACINE/"

echo "==> Environnement Python"
if [[ ! -d "$RACINE/.venv" ]]; then
  python3 -m venv "$RACINE/.venv"
fi
# piwheels evite de recompiler Pillow et numpy sur ARM : des heures gagnees.
"$RACINE/.venv/bin/pip" install --quiet --upgrade pip
"$RACINE/.venv/bin/pip" install --quiet \
  --extra-index-url https://www.piwheels.org/simple \
  -r "$RACINE/requirements.txt"

echo "==> Configuration"
if [[ ! -f "$RACINE/.env" ]]; then
  cp "$RACINE/.env.example" "$RACINE/.env"
  MOT_DE_PASSE="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  CLE="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  sed -i "s|^PHOTOGRAM_PASSWORD=.*|PHOTOGRAM_PASSWORD=$MOT_DE_PASSE|" "$RACINE/.env"
  sed -i "s|^PHOTOGRAM_SECRET_KEY=.*|PHOTOGRAM_SECRET_KEY=$CLE|" "$RACINE/.env"
  sed -i "s|^PHOTOGRAM_DATA_DIR=.*|PHOTOGRAM_DATA_DIR=$DONNEES|" "$RACINE/.env"
  echo
  echo "    Mot de passe genere : $MOT_DE_PASSE"
  echo "    (modifiable dans $RACINE/.env)"
  echo
else
  echo "    .env existant conserve."
fi
chmod 600 "$RACINE/.env"

echo "==> Repertoire de donnees"
mkdir -p "$DONNEES"
chown -R "$UTILISATEUR:$UTILISATEUR" "$DONNEES" "$RACINE"

echo "==> Services systemd"
cp "$SOURCE/deploy/photogram-web.service" "$SOURCE/deploy/photogram-worker.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now photogram-web photogram-worker

echo "==> Verification du swap"
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
PORT=$(grep -oP '^PHOTOGRAM_PORT=\K.*' "$RACINE/.env" || echo 8000)
echo
echo "Termine. Interface : http://${IP}:${PORT}"
echo "Installez ensuite la chaine de reconstruction : sudo ./scripts/install_pipeline.sh"
