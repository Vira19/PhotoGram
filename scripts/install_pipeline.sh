#!/usr/bin/env bash
# Installation de la chaine de reconstruction.
#
#   sudo ./scripts/install_pipeline.sh            # COLMAP via apt (recommande sur RPi)
#   sudo ./scripts/install_pipeline.sh --openmvg  # compile OpenMVG + OpenMVS (des heures)
#
# Pourquoi deux chemins ?
#
#   COLMAP est empaquete par Debian et Raspberry Pi OS : une minute
#   d'installation, et il produit un nuage de points epars en CPU. C'est ce
#   qu'un Raspberry Pi 3 peut realistiquement faire.
#
#   OpenMVG et OpenMVS ne sont pas empaquetes : il faut les compiler. C'est la
#   seule chaine qui va jusqu'au maillage texture sans GPU NVIDIA, mais compter
#   plusieurs heures de compilation sur un RPi3 -- et la compilation elle-meme
#   demande du swap.

set -euo pipefail

MODE=${1:-colmap}
SOURCES=/usr/local/src

ETAPE="demarrage"
trap 'echo >&2; echo "ECHEC pendant : $ETAPE (ligne $LINENO)" >&2; echo "Diagnostic : ./scripts/diagnose.sh" >&2; exit 1' ERR

if [[ $EUID -ne 0 ]]; then
  echo "Ce script doit etre lance avec sudo." >&2
  exit 1
fi

installer_colmap() {
  ETAPE="installation du paquet colmap"
  echo "==> Installation de COLMAP depuis les depots"
  apt-get update -qq
  if apt-get install -y --no-install-recommends colmap; then
    echo
    colmap --help >/dev/null 2>&1 && echo "COLMAP installe : $(command -v colmap)"
    echo
    echo "Le profil « Nuage epars seulement » est maintenant utilisable."
    echo "Pour un maillage texture : sudo $0 --openmvg"
  else
    cat >&2 <<'ECHEC'

    Le paquet colmap n'est pas disponible sur cette distribution.
    Verifiez la version de votre systeme (cat /etc/os-release) ; le paquet
    existe a partir de Debian 12 « bookworm ». A defaut, utilisez --openmvg.

ECHEC
    exit 1
  fi
}

installer_openmvg_openmvs() {
  RAM_MO=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  SWAP_MO=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
  if (( RAM_MO + SWAP_MO < 3000 )); then
    cat >&2 <<ECHEC

    Memoire insuffisante pour compiler : ${RAM_MO} Mo de RAM + ${SWAP_MO} Mo de swap.
    La compilation d'OpenMVS demande environ 3 Go (RAM + swap). Ajoutez du swap :

        sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
        sudo systemctl restart dphys-swapfile

ECHEC
    exit 1
  fi

  # Un seul job de compilation : sur un RPi3, g++ en parallele epuise la
  # memoire bien avant de saturer les quatre coeurs.
  JOBS=1
  (( RAM_MO > 4000 )) && JOBS=$(nproc)

  ETAPE="installation des dependances de compilation"
  echo "==> Dependances de compilation (compter un long moment)"
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    git cmake build-essential \
    libpng-dev libjpeg-dev libtiff-dev libxxf86vm1 libxxf86vm-dev libxi-dev libxrandr-dev \
    graphviz libboost-iostreams-dev libboost-program-options-dev libboost-serialization-dev \
    libboost-system-dev libopencv-dev libcgal-dev libatlas-base-dev libsuitesparse-dev \
    libeigen3-dev libglu1-mesa-dev freeglut3-dev

  mkdir -p "$SOURCES"

  ETAPE="compilation d'OpenMVG"
  echo "==> OpenMVG"
  if [[ ! -d "$SOURCES/openMVG" ]]; then
    git clone --recursive --depth 1 https://github.com/openMVG/openMVG.git "$SOURCES/openMVG"
  fi
  mkdir -p "$SOURCES/openMVG_build"
  cmake -S "$SOURCES/openMVG/src" -B "$SOURCES/openMVG_build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DOpenMVG_BUILD_TESTS=OFF \
    -DOpenMVG_BUILD_EXAMPLES=OFF \
    -DOpenMVG_BUILD_DOC=OFF
  cmake --build "$SOURCES/openMVG_build" -j "$JOBS"
  cmake --install "$SOURCES/openMVG_build"

  echo "==> VCGlib (dependance d'OpenMVS, non compilee)"
  [[ -d "$SOURCES/vcglib" ]] || git clone --depth 1 https://github.com/cdcseacave/VCG.git "$SOURCES/vcglib"

  ETAPE="compilation d'OpenMVS"
  echo "==> OpenMVS"
  if [[ ! -d "$SOURCES/openMVS" ]]; then
    git clone --recursive --depth 1 https://github.com/cdcseacave/openMVS.git "$SOURCES/openMVS"
  fi
  mkdir -p "$SOURCES/openMVS_build"
  cmake -S "$SOURCES/openMVS" -B "$SOURCES/openMVS_build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DVCG_ROOT="$SOURCES/vcglib" \
    -DOpenMVS_USE_CUDA=OFF \
    -DOpenMVS_BUILD_TOOLS=ON
  cmake --build "$SOURCES/openMVS_build" -j "$JOBS"
  cmake --install "$SOURCES/openMVS_build"

  ldconfig
  echo
  echo "Chaine complete installee. Verifiez sur la page « Etat du systeme »."
}

case "$MODE" in
  --openmvg|openmvg) installer_openmvg_openmvs ;;
  --colmap|colmap)   installer_colmap ;;
  *) echo "Usage : $0 [--colmap | --openmvg]" >&2; exit 1 ;;
esac

systemctl is-active --quiet photogram-worker && systemctl restart photogram-worker || true
