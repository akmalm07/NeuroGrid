param([switch]$Headless)

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
$Package = if ($Headless) { $Root } else { "$Root[desktop]" }
& $Python -m pip install --disable-pip-version-check $Package
if ($LASTEXITCODE -ne 0) { throw 'NeuroGrid installation failed.' }

if (-not $Headless) {
    $Programs = [Environment]::GetFolderPath('Programs')
    $null = New-Item -ItemType Directory -Force -Path $Programs
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut((Join-Path $Programs 'NeuroGrid Node.lnk'))
    $Shortcut.TargetPath = Join-Path $Environment 'Scripts/pythonw.exe'
    $Shortcut.Arguments = '-m neurogrid_node.desktop'
    $Shortcut.WorkingDirectory = $Root
    $Shortcut.Save()
    Write-Output 'Open NeuroGrid Node from the Start menu.'
}
Write-Output 'Terminal help: .\neurogrid-agent.cmd --help'
