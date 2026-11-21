#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
case "${1:-}" in
    '') PACKAGE="$ROOT[desktop]" ;;
    --headless) PACKAGE="$ROOT" ;;
    *) printf '%s\n' 'Usage: ./install-neurogrid.sh [--headless]' >&2; exit 2 ;;
esac
"${NEUROGRID_PYTHON:-python3.12}" -m venv "$ROOT/.venv"
PYTHON="$ROOT/.venv/bin/python"
"$PYTHON" -m pip install --disable-pip-version-check --index-url https://download.pytorch.org/whl/cpu 'torch==2.7.1+cpu'
"$PYTHON" -m pip install --disable-pip-version-check "$PACKAGE"
if [ "${1:-}" != --headless ]; then
    APPLICATIONS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    mkdir -p "$APPLICATIONS"
    "$PYTHON" - "$ROOT/start-neurogrid" "$APPLICATIONS/neurogrid-node.desktop" <<'PY'
import sys
from pathlib import Path

# Escape the Exec argument, then escape backslashes for the desktop entry value.
command = sys.argv[1].replace("\\", "\\\\")
for character in ('"', "`", "$"):
    command = command.replace(character, "\\" + character)
command = command.replace("%", "%%").replace("\\", "\\\\")
Path(sys.argv[2]).write_text(
    "[Desktop Entry]\nType=Application\nName=NeuroGrid Node\n"
    "Comment=Volunteer CPU inference for NeuroGrid\n"
    f'Exec="{command}"\nTerminal=false\nCategories=Utility;\n',
    encoding="utf-8",
)
PY
    printf '%s\n' 'Open NeuroGrid Node from the applications menu.'
fi
printf '%s\n' 'Terminal help: ./neurogrid-agent --help'
