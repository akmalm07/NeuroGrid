@echo off
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Run Install-NeuroGrid.ps1 before starting the node.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" -m neurogrid_node.desktop %*
