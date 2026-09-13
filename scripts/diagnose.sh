#!/usr/bin/env bash
# Rapport de diagnostic PhotoGram.
#
#   ./scripts/diagnose.sh
#
# N'exige pas les droits root et ne modifie rien. Le rapport ne contient ni
# mot de passe ni cle : il peut etre copie tel quel dans un ticket.

set -uo pipefail   # volontairement sans -e : un test qui echoue est une info

RACINE=/opt/photogram
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

titre() { echo; echo "=== $1 ==="; }
verdict() { if [[ $1 -eq 0 ]]; then echo "  [ok]   $2"; else echo "  [KO]   $2"; fi; }

echo "Rapport de diagnostic PhotoGram — $(date '+%Y-%m-%d %H:%M')"

titre "Systeme"
echo "  Distribution : $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || echo inconnue)"
echo "  Noyau        : $(uname -r)"
echo "  Architecture : $(uname -m)"
if [[ -f /proc/device-tree/model ]]; then
  echo "  Modele       : $(tr -d '\0' < /proc/device-tree/model)"
fi
echo "  Coeurs       : $(nproc)"
awk '/MemTotal|SwapTotal/ {printf "  %-12s : %d Mio\n", $1, $2/1024}' /proc/meminfo

# Le manque de swap est la premiere cause d'echec sur un Pi : ni la
# compilation des dependances ni la densification ne tiennent sans lui.
RAM_MO=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
SWAP_MO=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
if (( RAM_MO < 1536 && SWAP_MO < 1024 )); then
  echo "  [KO]   Swap insuffisant pour cette quantite de RAM (voir README)"
fi

ARCH=$(uname -m)
if [[ "$ARCH" == armv6l || "$ARCH" == armv7l ]]; then
  echo "  [KO]   Systeme 32 bits : plusieurs dependances Python n'ont pas de"
  echo "         version precompilee et devront etre compilees (Rust requis)."
  echo "         Un Raspberry Pi 3 peut faire tourner Raspberry Pi OS 64 bits."
fi

titre "Python"
if command -v python3 >/dev/null; then
  echo "  $(python3 --version 2>&1) — $(command -v python3)"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
  verdict $? "version >= 3.9 requise"
else
  echo "  [KO]   python3 introuvable"
fi
python3 -c 'import venv' 2>/dev/null
verdict $? "module venv disponible (paquet python3-venv)"

titre "Outils utilises par l'installation"
for outil in tar sed awk useradd systemctl apt-get; do
  command -v "$outil" >/dev/null
  verdict $? "$outil"
done

titre "Installation"
if [[ -d "$RACINE" ]]; then
  echo "  Racine       : $RACINE"
  [[ -f "$RACINE/.env" ]]; verdict $? "$RACINE/.env present"
  [[ -x "$RACINE/.venv/bin/python" ]]; verdict $? "environnement virtuel present"

  if [[ -x "$RACINE/.venv/bin/python" ]]; then
    echo "  Paquets installes :"
    "$RACINE/.venv/bin/pip" list --format=freeze 2>/dev/null \
      | grep -iE '^(fastapi|starlette|uvicorn|pillow|jinja2|pydantic|python-multipart|itsdangerous)' \
      | sed 's/^/    /'
    "$RACINE/.venv/bin/python" -c 'import app.main' 2>/dev/null
    verdict $? "l'application s'importe sans erreur"
  fi

  if [[ -f "$RACINE/.env" ]] && [[ -r "$RACINE/.env" ]]; then
    DONNEES=$(grep -E '^PHOTOGRAM_DATA_DIR=' "$RACINE/.env" | cut -d= -f2-)
    echo "  Donnees      : ${DONNEES:-non defini}"
    if [[ -n "${DONNEES:-}" ]]; then
      [[ -d "$DONNEES" ]]; verdict $? "le repertoire de donnees existe"
      # Une incoherence ici rend le service muet : systemd monte tout le
      # disque en lecture seule sauf les chemins declares.
      if [[ -f /etc/systemd/system/photogram-worker.service ]]; then
        grep -q "^ReadWritePaths=$DONNEES$" /etc/systemd/system/photogram-worker.service
        verdict $? "ReadWritePaths du service correspond au repertoire de donnees"
      fi
    fi
  else
    echo "  (.env illisible sans sudo — relancez avec sudo pour ces controles)"
  fi
else
  echo "  PhotoGram n'est pas installe dans $RACINE."
  echo "  Depot courant : $SOURCE"
fi

titre "Services"
for unite in photogram-web photogram-worker; do
  if systemctl list-unit-files "$unite.service" &>/dev/null \
     && [[ -f "/etc/systemd/system/$unite.service" ]]; then
    echo "  $unite : $(systemctl is-active "$unite" 2>/dev/null) / $(systemctl is-enabled "$unite" 2>/dev/null)"
    if ! systemctl is-active --quiet "$unite"; then
      echo "    Dernieres lignes du journal :"
      journalctl -u "$unite" -n 15 --no-pager 2>/dev/null | sed 's/^/      /' \
        || echo "      (journal inaccessible sans sudo)"
    fi
  else
    echo "  $unite : non installe"
  fi
done

titre "Chaine de reconstruction"
for binaire in colmap openMVG_main_SfMInit_ImageListing openMVG_main_SfM \
               DensifyPointCloud ReconstructMesh TextureMesh; do
  CHEMIN=$(command -v "$binaire" 2>/dev/null \
    || ls /usr/local/bin/openMVG/"$binaire" /usr/local/bin/OpenMVS/"$binaire" 2>/dev/null | head -1)
  if [[ -n "$CHEMIN" ]]; then
    echo "  [ok]   $binaire"
  else
    echo "  [--]   $binaire (absent)"
  fi
done
echo
echo "  Rappel : COLMAP seul suffit au profil « Nuage epars seulement »."

titre "Reseau"
PORT=$(grep -E '^PHOTOGRAM_PORT=' "$RACINE/.env" 2>/dev/null | cut -d= -f2-)
PORT=${PORT:-8000}
if command -v curl >/dev/null; then
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/login" 2>/dev/null)
  [[ "$CODE" == "200" ]]; verdict $? "le site repond sur http://127.0.0.1:$PORT (code ${CODE:-aucun})"
fi
echo "  Adresses : $(hostname -I 2>/dev/null || echo inconnue)"

titre "Espace disque"
df -h "${DONNEES:-/}" 2>/dev/null | sed 's/^/  /'

echo
echo "Fin du rapport."
