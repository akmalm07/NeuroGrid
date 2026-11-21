# Install NeuroGrid Node

Use 64-bit Windows 10 or 11, or an x86-64 Linux desktop such as Ubuntu 24.04. Allow 4 GB of RAM and 4 GB of free disk space. Install Python 3.12 with the Python Launcher on Windows, or with the `venv` package on Linux. A GPU is optional.

Extract the archive to a permanent folder. Open a terminal in that folder.

- On Windows run `powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-NeuroGrid.ps1`. Search for **NeuroGrid Node** in Start after installation. You can also use `Start NeuroGrid.cmd`. The execution policy setting applies to the installer process.
- On Linux run `./install-neurogrid.sh`. Search for **NeuroGrid Node** in your applications menu. You can also run `./start-neurogrid`. If Qt cannot load the desktop backend, install your distribution's Qt XCB runtime dependencies, including `libxcb-cursor0` on Ubuntu.

These installers create a local Python environment and download PySide6 and CPU PyTorch. On first connect the app downloads the pinned SmolLM2 checkpoint. The Windows model cache sits under `%LOCALAPPDATA%\NeuroGrid\models`. Each node loads only its assigned weights into the inference runtime.

In the NeuroGrid website's **Downloads** page, sign in, create a node key, and paste it and the site address into the desktop app. Set the CPU thread count and approximate compute duty cycle, then select **Start**. Use **Pause** or **Quit** in the window or the system tray. Tray support depends on the desktop environment. The app uses the operating-system keyring when one is available. Otherwise the node key needs to be reentered after relaunch.

Check **Launch and resume when I sign in** to enable automatic startup. Clear it before moving or removing the extracted folder. Revoke unused keys in Downloads.

## Run without a desktop

The headless installer skips Qt and desktop registration. Create a node key on the website's **Downloads** page. From the extracted folder, run the commands for your system.

Windows PowerShell

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-NeuroGrid.ps1 -Headless
$env:NEUROGRID_SECRET = [System.Net.NetworkCredential]::new('', (Read-Host 'Node key' -AsSecureString)).Password
.\neurogrid-agent.cmd node --url https://neurogrid.app.space --threads 2 --duty 50
Remove-Item Env:NEUROGRID_SECRET
```

Linux Bash

```bash
./install-neurogrid.sh --headless
read -rsp 'Node key: ' NEUROGRID_SECRET; printf '\n'
NEUROGRID_SECRET="$NEUROGRID_SECRET" ./neurogrid-agent node --url https://neurogrid.app.space --threads 2 --duty 50
unset NEUROGRID_SECRET
```

The agent prints its node ID, assigned partition, and work status. Use `--help` to see options and Ctrl+C to stop. On Linux, a service manager can run the same foreground command and stop it with SIGTERM. Use a separate node key per process.
