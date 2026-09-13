#!/usr/bin/env bash
# Lancement en developpement : web et worker dans le meme terminal.
#
#   ./scripts/dev.sh
#
# Le serveur recharge automatiquement le code ; le worker, non : relancez-le
# a la main apres avoir touche au pipeline.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -d .venv ]]; then
  echo "==> Creation de l'environnement virtuel"
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements-dev.txt
fi

if [[ ! -f .env ]]; then
  echo "==> Creation d'un .env de developpement"
  cp .env.example .env
  sed -i "s|^PHOTOGRAM_PASSWORD=.*|PHOTOGRAM_PASSWORD=photogram|" .env
  sed -i "s|^PHOTOGRAM_SECRET_KEY=.*|PHOTOGRAM_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')|" .env
  sed -i "s|^PHOTOGRAM_DATA_DIR=.*|PHOTOGRAM_DATA_DIR=$(pwd)/data|" .env
  echo "    Mot de passe : photogram"
fi

# Le worker est arrete en meme temps que le serveur.
.venv/bin/python -m app.worker &
PID_WORKER=$!
trap 'kill $PID_WORKER 2>/dev/null || true' EXIT

.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
