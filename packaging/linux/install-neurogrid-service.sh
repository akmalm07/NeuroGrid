#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
app=/opt/neurogrid-contributor
config=/etc/neurogrid-contributor
state=/var/lib/neurogrid-contributor
name=neurogrid-contributor.service
unit=/etc/systemd/system/$name
user=neurogrid-contributor

[[ $EUID == 0 && -d /run/systemd/system ]] || {
    printf '%s\n' 'Run the Linux beta service installer as root on a systemd host.' >&2
    exit 1
}

if [[ ${1:-} == --uninstall-service ]]; then
    systemctl disable --now "$name"
    rm -f -- "$unit"
    systemctl daemon-reload
    printf '%s\n' 'Service removed. Its profile, enrollment and cache were retained.'
    exit
fi

if ! id "$user" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$state" --shell /usr/sbin/nologin "$user"
fi
install -d -m 0755 "$app" "$app/neurogrid_node"
install -d -m 0700 "$config"
install -d -m 0700 -o "$user" -g "$user" "$state"
if [[ $root != "$app" ]]; then
    install -m 0755 "$root/install-neurogrid.sh" "$app/install-neurogrid.sh"
    install -m 0644 "$root/pyproject.toml" "$app/pyproject.toml"
    install -m 0644 "$root"/neurogrid_node/*.{py,json} "$app/neurogrid_node/"
fi
if systemctl is-active --quiet "$name"; then
    systemctl stop "$name"
fi
"$app/install-neurogrid.sh" --headless
python="$app/.venv/bin/python"
if [[ ! -f "$state/profile.json" ]]; then
    read -rp 'NeuroGrid site [https://neurogrid.app.space]: ' site </dev/tty
    runuser -u "$user" -- "$python" -m neurogrid_node.client config --profile "$state/profile.json" set \
        --url "${site:-https://neurogrid.app.space}" >/dev/null
fi
if [[ ! -s "$config/node.key" || ${1:-} == --rotate-service-key ]]; then
    read -rsp 'Paste the service node key: ' key </dev/tty
    printf '\n' >&2
    [[ $key =~ ^ng_[A-Za-z0-9-]{1,190}$ ]] || { printf '%s\n' 'Invalid node key format.' >&2; exit 1; }
    printf '%s' "$key" | runuser -u "$user" -- "$python" -c '
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
' "$state/profile.json"
    temporary=$(mktemp "$config/.node-key-XXXXXX")
    printf '%s\n' "$key" > "$temporary"
    mv -f -- "$temporary" "$config/node.key"
    unset key
fi
cat > "$unit" <<'UNIT'
[Unit]
Description=NeuroGrid Granite contributor (Linux beta)
After=network-online.target
Wants=network-online.target

[Service]
User=neurogrid-contributor
Group=neurogrid-contributor
Type=exec
StateDirectory=neurogrid-contributor
StateDirectoryMode=0700
LoadCredential=node.key:/etc/neurogrid-contributor/node.key
ExecStart=/opt/neurogrid-contributor/.venv/bin/python -I -m neurogrid_node.service --profile /var/lib/neurogrid-contributor/profile.json --cache /var/lib/neurogrid-contributor/models
Restart=on-failure
RestartSec=8
RestartPreventExitStatus=78
TimeoutStopSec=60
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "$name"
printf '%s\n' "Status: systemctl status $name" "Logs: journalctl -u $name"
