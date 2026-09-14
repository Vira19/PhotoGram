#!/usr/bin/env bash
# Developpement : prepare l'environnement puis lance PhotoGram avec
# rechargement automatique du code.
#
#   ./scripts/dev.sh
#
# Pour un usage normal sur sa machine, « python -m app.run » suffit : ce script
# n'ajoute que la creation du venv de developpement et l'option --dev.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -d .venv ]]; then
  echo "==> Creation de l'environnement virtuel"
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements-dev.txt
fi

exec .venv/bin/python -m app.run --dev "$@"
