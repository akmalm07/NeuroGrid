param([switch]$RotateKey, [switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$name = 'NeuroGridContributor'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$admin = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run the service installer in an elevated PowerShell window.'
}
$service = Get-CimInstance Win32_Service -Filter "Name='$name'"
if ($Uninstall) {
    if ($service) {
        if ($service.State -ne 'Stopped') { Stop-Service -Name $name }
        $result = Invoke-CimMethod -InputObject $service -MethodName Delete
        if ($result.ReturnValue -ne 0) { throw "Service removal failed ($($result.ReturnValue))." }
    }
    Write-Output 'Service removed. Its profile, enrollment and cache were retained.'
    return
}

$root = (Get-Item -LiteralPath $PSScriptRoot).FullName
$expected = Join-Path $env:ProgramFiles 'NeuroGrid'
if ($root -ne $expected -or (Get-Item -LiteralPath $root).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
    throw "Extract the Windows package to $expected before installing the service."
}
$basePython = Join-Path $env:ProgramFiles 'Python312\python.exe'
if (-not (Test-Path -LiteralPath $basePython) -or (Get-AuthenticodeSignature $basePython).Status -ne 'Valid') {
    throw 'Install signed Python 3.12 for all users in Program Files before installing the service.'
}
if ($service -and $service.State -ne 'Stopped') { Stop-Service -Name $name }
$oldPython = $env:NEUROGRID_PYTHON
try {
    $env:NEUROGRID_PYTHON = $basePython
    & (Join-Path $root 'Install-NeuroGrid.ps1') -Headless
} finally {
    $env:NEUROGRID_PYTHON = $oldPython
}
$python = Join-Path $root '.venv\Scripts\python.exe'

$state = Join-Path $env:ProgramData $name
$data = New-Item -ItemType Directory -Path $state -Force
if ($data.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { throw 'The service data path must be a directory.' }
$profile = Join-Path $state 'profile.json'
$keyFile = Join-Path $state 'node.key'
$cache = Join-Path $state 'models'
$entryPoint = Join-Path $root 'ServiceHost.py'
$command = '"' + $basePython + '" -I -u "' + $entryPoint + '" --windows --profile "' +
    $profile + '" --credential "' + $keyFile + '" --cache "' + $cache + '"'
if (-not $service) {
    $result = Invoke-CimMethod -ClassName Win32_Service -MethodName Create -Arguments @{
        Name = $name
        DisplayName = 'NeuroGrid Granite contributor'
        PathName = $command
        ServiceType = [byte]16
        ErrorControl = [byte]1
        StartMode = 'Disabled'
        DesktopInteract = $false
        StartName = 'NT AUTHORITY\LocalService'
    }
    if ($result.ReturnValue -ne 0) { throw "Service creation failed ($($result.ReturnValue))." }
}
& sc.exe sidtype $name unrestricted | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not configure the service SID.' }
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$sid = [Security.Principal.NTAccount]::new('NT SERVICE', $name).Translate([Security.Principal.SecurityIdentifier])
foreach ($account in @($admins, $system, $sid)) {
    $rights = if ($account -eq $sid) { 'Modify' } else { 'FullControl' }
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $account, $rights, 'ContainerInherit,ObjectInherit', 'None', 'Allow'
    )
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $state -AclObject $acl
if (-not (Test-Path -LiteralPath $profile)) {
    $site = Read-Host 'NeuroGrid site [https://neurogrid.app.space]'
    if (-not $site) { $site = 'https://neurogrid.app.space' }
    & $python -I -m neurogrid_node.client config --profile $profile set --url $site | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Service profile could not be saved.' }
}
if ($RotateKey -or -not (Test-Path -LiteralPath $keyFile)) {
    $secureKey = Read-Host 'Paste the service node key' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try {
        $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Trim()
        if ($key -notmatch '^ng_[A-Za-z0-9-]{1,190}$') { throw 'Invalid node key format.' }
        $key | & $python -I -c @'
import sys
from pathlib import Path
from neurogrid_node import RUNTIME
from neurogrid_node.client import GridClient
from neurogrid_node.profile import ProfileStore
try:
    site = ProfileStore(Path(sys.argv[1])).read().site
    if GridClient(site, sys.stdin.read()).call("profile").get("runtime") != RUNTIME:
        sys.exit("The coordinator is not serving the Granite runtime.")
except Exception:
    sys.exit("The service node key could not be verified.")
'@ $profile
        if ($LASTEXITCODE -ne 0) { throw 'Service enrollment could not be verified.' }
        $temporary = Join-Path $state ('.key-' + [guid]::NewGuid().ToString('N'))
        try {
            [IO.File]::WriteAllText($temporary, $key, [Text.Encoding]::ASCII)
            if (Test-Path -LiteralPath $keyFile) {
                [IO.File]::Replace($temporary, $keyFile, $null)
            } else {
                [IO.File]::Move($temporary, $keyFile)
            }
        } finally {
            if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
        }
    } finally {
        $key = $null
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}
$service = Get-CimInstance Win32_Service -Filter "Name='$name'"
$result = Invoke-CimMethod -InputObject $service -MethodName Change -Arguments @{
    PathName = $command
    StartMode = 'Automatic'
}
if ($result.ReturnValue -ne 0) { throw "Service startup configuration failed ($($result.ReturnValue))." }
& sc.exe failure $name reset= 86400 actions= restart/8000/restart/8000/restart/30000 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Service recovery could not be configured.' }
& sc.exe failureflag $name 1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Service failure reporting could not be configured.' }
Start-Service -Name $name
Write-Output "Status: Get-Service $name"
