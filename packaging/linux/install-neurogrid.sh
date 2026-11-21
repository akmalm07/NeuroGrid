#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
"${NEUROGRID_PYTHON:-python3.12}" -m venv "$ROOT/.venv"
PYTHON="$ROOT/.venv/bin/python"
"$PYTHON" -m pip install --disable-pip-version-check --index-url https://download.pytorch.org/whl/cpu 'torch==2.7.1+cpu'
"$PYTHON" -m pip install --disable-pip-version-check "$ROOT[desktop]"
printf '%s\n' 'Installation complete. Run ./start-neurogrid to launch.'
