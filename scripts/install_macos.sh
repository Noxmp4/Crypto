#!/bin/bash
# Installation de daybot sur macOS (Intel ou Apple Silicon).
# Crée .venv/, installe les dépendances et lance les tests. Relançable sans risque.
# Compatible avec le bash 3.2 livré par macOS.
set -eu

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Ce script est prévu pour macOS (uname = $(uname -s))." >&2
fi

# Choix de l'interpréteur : le plus récent disponible (Homebrew, python.org, puis système).
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$(command -v "$cand")"
    break
  fi
done

if [ -z "$PY" ]; then
  cat >&2 <<'MSG'
Python 3.9+ introuvable. Deux options :
  1. Homebrew (recommandé) :  brew install python@3.12
  2. Outils Apple :           xcode-select --install   (fournit /usr/bin/python3 3.9)
Puis relancez ce script.
MSG
  exit 1
fi

echo "→ Python : $PY ($("$PY" -c 'import platform; print(platform.python_version(), platform.machine())'))"
if [ "$PY" = "/usr/bin/python3" ]; then
  echo "  (Python système d'Apple : fonctionne, mais Homebrew python@3.12 est plus rapide et à jour.)"
fi

if [ ! -x .venv/bin/python ]; then
  echo "→ Création de l'environnement virtuel .venv"
  "$PY" -m venv .venv
fi

echo "→ Installation des dépendances"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

echo "→ Tests"
.venv/bin/python -m pytest -q tests

mkdir -p data
cat <<MSG

✅ Installation terminée dans $ROOT

  Backtest :    scripts/run_macos.sh backtest --days 180
  Paper :       scripts/run_macos.sh paper
  Statut :      scripts/run_macos.sh status
  Clé API :     scripts/run_macos.sh keychain-import ~/Downloads/cdp_api_key.json
MSG
