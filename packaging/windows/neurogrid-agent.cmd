@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Run Install-NeuroGrid.ps1 before starting the node. >&2
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m neurogrid_node.client %*
exit /b %errorlevel%
