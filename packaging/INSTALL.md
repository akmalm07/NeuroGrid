# Install NeuroGrid Node

The source packages serve **IBM Granite 4.2 8B**. Linux is **beta**. Use x86-64 Windows 10 or 11, or Ubuntu 24.04 or compatible Linux with Python 3.12 and venv. Allow at least 12 GB RAM for one contributor and 25 GB disk for the runtime and pinned checkpoint.

## Desktop and headless

Extract the archive to a permanent folder. Run `powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-NeuroGrid.ps1` on Windows or `./install-neurogrid.sh` on Linux. Open **NeuroGrid Node** from the application menu. Linux desktop users may need their distribution's Qt XCB dependencies.

Choose **Settings → Compute limits**, set the site and CPU limits, and select **Save**. Paste a node key from Downloads, then select **Start**. Settings are also available from the tray during compute. Pause and restart the worker to apply saved changes. The sign-in startup option controls the tray app, and the OS keyring stores its credential when available.

Use `-Headless` on Windows or `--headless` on Linux to install without Qt. The launcher is `.\neurogrid-agent.cmd` on Windows and `./neurogrid-agent` on Linux.

```text
neurogrid-agent config show
neurogrid-agent config set --threads 2 --duty 50
neurogrid-agent status
neurogrid-agent node
```

Supply `NEUROGRID_SECRET` in the worker process environment. Use a silent input prompt and clear the variable when finished. Config and status commands redact credentials.

The default profile is `%LOCALAPPDATA%\NeuroGrid\profile.json` on Windows and `${XDG_CONFIG_HOME:-$HOME/.config}/neurogrid/profile.json` on Linux. First use imports the current user's saved site, CPU, duty cycle, and sign-in preference. Use `config --profile PATH show`, `status --profile PATH`, or `node --profile PATH` for a separate profile. Use a distinct node key per worker.

## Boot service

A boot worker has its own profile, key, and model cache. Enroll it using the compatible local or staging coordinator.

### Windows

Install signed Python 3.12 for all users in `%ProgramFiles%\Python312`. Extract the package to `%ProgramFiles%\NeuroGrid` and open elevated PowerShell there.

```powershell
.\Install-NeuroGrid.ps1 -BootService
Get-Service NeuroGridContributor
Get-Content "$env:ProgramData\NeuroGridContributor\service.log" -Tail 25
.\neurogrid-agent.cmd status --profile "$env:ProgramData\NeuroGridContributor\profile.json"
```

The SCM runs the signed Python executable with the NeuroGrid dispatcher under LocalService and a per-service SID. The installation prompts for the site and node key on first use. Use `-RotateServiceKey` to re-enroll and `-UninstallBootService` to remove the service. The profile and model cache are retained.

### Linux beta

Use root on a systemd system and enter the service site and key when prompted.

```sh
sudo ./install-neurogrid.sh --service
sudo systemctl status neurogrid-contributor.service
sudo journalctl -u neurogrid-contributor.service
sudo ./neurogrid-agent status --profile /var/lib/neurogrid-contributor/profile.json
```

Use `--rotate-service-key` to replace enrollment or `--uninstall-service` to remove the unit. The service account owns its profile and cache, and systemd loads the credential from a root-protected file.

On either OS edit the service profile from an elevated shell. Use `config --profile PATH set --pause` and restart the service to persist a pause. Use `--resume` and restart it to share compute again. Keys expire after 30 days and can be revoked from Downloads.

### Run all development roles on a Linux host

This Linux beta installer needs the reference key and four distinct worker keys. Keep the local archive and checksum together.

```sh
sudo bash packaging/linux/install-neurogrid-host.sh /path/to/NeuroGrid-Node-Linux-x64.tar.gz
sudo systemctl status neurogrid-worker-{1,2,3,4}.service neurogrid-reference.service
```

The four workers and reference share the pinned model cache. See the [local test runbook](../README.md#local-development) for isolated configuration.
