#!/usr/bin/env bash
set -euo pipefail

VERSION=v0.1.5
SITE=https://neurogrid.app.space
APP_DIR="/opt/neurogrid/$VERSION"
CONFIG_DIR=/etc/neurogrid
STATE_DIR=/var/lib/neurogrid
RELEASE_URL="https://github.com/akmalm07/NeuroGrid/releases/download/$VERSION"
ARCHIVE=NeuroGrid-Node-Linux-x64.tar.gz
PYTHON="${NEUROGRID_PYTHON:-python3.12}"

fail() { printf '%s\n' "$*" >&2; exit 1; }

[[ $EUID == 0 ]] || fail "Run this installer with sudo."
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || fail "This release requires x86-64 Linux."
[[ -d /run/systemd/system ]] || fail "This installer requires systemd."
for command in "$PYTHON" curl tar sha256sum runuser useradd systemctl; do
    command -v "$command" >/dev/null || fail "Required command unavailable: $command"
done

if ! id neurogrid >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin neurogrid
fi
install -d -m 0755 "$APP_DIR"
install -d -m 0700 "$CONFIG_DIR"
install -d -m 0750 -o neurogrid "$STATE_DIR"

if [[ ! -f "$APP_DIR/.headless-installed" ]]; then
    download=$(mktemp -d)
    trap 'rm -f -- "$download/$ARCHIVE" "$download/$ARCHIVE.sha256"; rmdir -- "$download"' EXIT
    curl --fail --silent --show-error --location "$RELEASE_URL/$ARCHIVE" -o "$download/$ARCHIVE"
    curl --fail --silent --show-error --location "$RELEASE_URL/$ARCHIVE.sha256" -o "$download/$ARCHIVE.sha256"
    (cd "$download" && sha256sum --check "$ARCHIVE.sha256")
    tar -xzf "$download/$ARCHIVE" -C "$APP_DIR"
    NEUROGRID_PYTHON="$PYTHON" "$APP_DIR/install-neurogrid.sh" --headless
    touch "$APP_DIR/.headless-installed"
fi

# Validate all three node credentials against the expected partition before starting services.
labels=(Input Transform Output)
for shard in 0 1 2; do
    key_file="$CONFIG_DIR/worker-$((shard + 1)).key"
    if [[ ! -s "$key_file" ]]; then
        read -rsp "Paste the ${labels[shard]} partition key: " key </dev/tty
        printf '\n' >&2
        [[ -n "$key" ]] || fail "A node key is required."
        new_key=true
    else
        key=$(<"$key_file")
        new_key=false
    fi
    printf '%s' "$key" | "$APP_DIR/.venv/bin/python" -c '
import sys
from neurogrid_node.client import GridClient, GridError

site, shard = sys.argv[1:]
try:
    actual = GridClient(site, sys.stdin.read()).call("profile")["shard"]
except (GridError, OSError, ValueError) as exc:
    sys.exit(f"Could not verify node key: {exc}")
if actual != int(shard):
    sys.exit(f"This key hosts partition {actual + 1}. Use the key for partition {int(shard) + 1}.")
' "$SITE" "$shard"
    if $new_key; then
        (umask 077; printf '%s\n' "$key" >"$key_file")
    fi
    unset key
done

if [[ ! -s "$CONFIG_DIR/reference.key" ]]; then
    read -rsp 'Paste the NEUROGRID_VERIFIER_SECRET configured on DeepSpace: ' key </dev/tty
    printf '\n' >&2
    [[ -n "$key" ]] || fail "The verifier secret is required."
    (umask 077; printf '%s\n' "$key" >"$CONFIG_DIR/reference.key")
    unset key
fi

printf '%s\n' "Preparing the shared SmolLM2 cache..."
runuser -u neurogrid -- env HF_HOME="$STATE_DIR/huggingface" "$APP_DIR/.venv/bin/python" \
    -c 'from neurogrid_node.model import checkpoint; checkpoint()'

# Load credentials from systemd's private per-service directory.
cat >"$APP_DIR/run-service" <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
export NEUROGRID_SECRET
NEUROGRID_SECRET=$(<"$CREDENTIALS_DIRECTORY/grid-key")
exec "$@"
RUNNER
chmod 0755 "$APP_DIR/run-service"

configure_service() {
    local name=$1 role=$2
    sed -e "s|@NAME@|$name|g" -e "s|@ROLE@|$role|g" -e "s|@APP@|$APP_DIR|g" \
        -e "s|@SITE@|$SITE|g" -e "s|@STATE@|$STATE_DIR|g" -e "s|@CONFIG@|$CONFIG_DIR|g" \
        >"/etc/systemd/system/neurogrid-$name.service" <<'UNIT'
[Unit]
Description=NeuroGrid @NAME@
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
User=neurogrid
Environment=HF_HOME=@STATE@/huggingface
LoadCredential=grid-key:@CONFIG@/@NAME@.key
ExecStart=@APP@/run-service @APP@/neurogrid-agent @ROLE@ --url @SITE@ --threads 1
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT
}

configure_service worker-1 node
configure_service worker-2 node
configure_service worker-3 node
configure_service reference reference
systemctl daemon-reload
systemctl enable --now neurogrid-worker-{1,2,3}.service neurogrid-reference.service
printf '\nServices started. Watch for three online partitions and the verifier at %s/network\n' "$SITE"
printf '%s\n' "Logs: sudo journalctl -u 'neurogrid-*.service' -f"
