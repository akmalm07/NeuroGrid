$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Environment = Join-Path $Root '.venv'

if ($env:NEUROGRID_PYTHON) {
    & $env:NEUROGRID_PYTHON -m venv $Environment
} else {
    py -3.12 -m venv $Environment
}
if ($LASTEXITCODE -ne 0) { throw 'Install x86-64 Python 3.12 with the Python Launcher and retry.' }

$Python = Join-Path $Environment 'Scripts/python.exe'
& $Python -m pip install --disable-pip-version-check --index-url https://download.pytorch.org/whl/cpu 'torch==2.7.1+cpu'
if ($LASTEXITCODE -ne 0) { throw 'CPU PyTorch installation failed.' }
& $Python -m pip install --disable-pip-version-check "$Root[desktop]"
if ($LASTEXITCODE -ne 0) { throw 'NeuroGrid installation failed.' }
Write-Output 'Installation complete. Open Start NeuroGrid.cmd to launch.'
