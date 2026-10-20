#!/bin/bash
# Lance daybot avec le Python de .venv. Pour paper/live, `caffeinate` empêche le Mac
# de se mettre en veille tant que le bot tourne (Ctrl+C pour arrêter les deux).
#   scripts/run_macos.sh paper
#   scripts/run_macos.sh backtest --days 60
set -eu

cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo "Environnement absent : lancez d'abord scripts/install_macos.sh" >&2
  exit 1
fi

CMD="${1:-}"
if [ -z "$CMD" ]; then
  exec .venv/bin/python -m daybot --help
fi

if { [ "$CMD" = "paper" ] || [ "$CMD" = "live" ]; } && command -v caffeinate >/dev/null 2>&1; then
  # -i : pas de veille sur inactivité ; -s : pas de veille système sur secteur.
  # Fermer le capot d'un MacBook sans écran externe met quand même le Mac en veille.
  exec caffeinate -is .venv/bin/python -m daybot "$@"
fi
exec .venv/bin/python -m daybot "$@"
