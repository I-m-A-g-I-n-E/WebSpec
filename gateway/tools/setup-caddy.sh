#!/usr/bin/env bash
#
# setup-caddy.sh — Install Caddy with rate-limit plugin, configure as reverse proxy
# for WebSpec gateway. Run once to set up the Caddy layer.
#
# Usage: sudo bash gateway/tools/setup-caddy.sh
#
set -euo pipefail

CADDY_PORT=7001
GATEWAY_PORT=7002
DOMAIN="${WEBSPEC_DOMAIN:-i-a-m.live}"
GATEWAY_USER="${SUDO_USER:-$USER}"
GATEWAY_HOME=$(eval echo "~${GATEWAY_USER}")

echo "=== WebSpec Caddy Setup ==="
echo "  Caddy port:   ${CADDY_PORT}"
echo "  Gateway port: ${GATEWAY_PORT}"
echo "  Domain:       ${DOMAIN}"
echo "  User:         ${GATEWAY_USER}"
echo ""

# ── 1. Install Go if missing ──
if ! command -v go &>/dev/null; then
    echo "Installing Go from official tarball..."
    GO_VERSION="1.22.5"
    curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o /tmp/go.tar.gz
    rm -rf /usr/local/go
    tar -C /usr/local -xzf /tmp/go.tar.gz
    rm /tmp/go.tar.gz
    export PATH="/usr/local/go/bin:$PATH"
fi
export PATH="/usr/local/go/bin:$(eval echo ~${GATEWAY_USER})/go/bin:$PATH"
echo "Go: $(go version)"

# ── 2. Build Caddy with rate-limit plugin ──
if ! command -v xcaddy &>/dev/null; then
    echo "Installing xcaddy..."
    GOBIN=/usr/local/bin go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
fi

echo "Building Caddy with caddy-ratelimit plugin..."
TMPDIR=$(mktemp -d)
cd "$TMPDIR"
xcaddy build --with github.com/mholt/caddy-ratelimit
mv caddy /usr/local/bin/caddy
chmod +x /usr/local/bin/caddy
cd /
rm -rf "$TMPDIR"
echo "Caddy: $(/usr/local/bin/caddy version)"

# ── 3. Create directories ──
mkdir -p /etc/caddy/conf.d
mkdir -p /var/log/caddy
chown "${GATEWAY_USER}:${GATEWAY_USER}" /var/log/caddy

# ── 4. Write Caddyfile ──
cat > /etc/caddy/Caddyfile <<EOF
{
    admin localhost:2019
    auto_https off
}

import /etc/caddy/conf.d/*.caddy

# Catch-all for unknown subdomains
:${CADDY_PORT} {
    respond "Unknown service" 421
}
EOF
echo "Wrote /etc/caddy/Caddyfile"

# ── 5. Generate site blocks for existing services ──
# Read service names from ~/.claude.json
CLAUDE_CONFIG="${GATEWAY_HOME}/.claude.json"
if [ -f "$CLAUDE_CONFIG" ]; then
    echo "Generating site blocks from ${CLAUDE_CONFIG}..."
    # Use python to extract service names (already installed for gateway)
    python3 -c "
import json, sys
sys.path.insert(0, '${GATEWAY_HOME}/MCP/webspec-gateway')
from webspec.config import parse_claude_config
from webspec.caddy import generate_site_block, write_site_block
from pathlib import Path

services = parse_claude_config(Path('${CLAUDE_CONFIG}'))
for name, entry in services.items():
    content = generate_site_block(
        name=name, domain='${DOMAIN}',
        gateway_port=${GATEWAY_PORT}, guard=entry.guard,
        caddy_port=${CADDY_PORT},
    )
    write_site_block(name, content)
    print(f'  {name}.caddy')
"
else
    echo "Warning: ${CLAUDE_CONFIG} not found, skipping site block generation"
fi

# ── 6. Create Caddy systemd service ──
cat > /etc/systemd/system/caddy-webspec.service <<EOF
[Unit]
Description=Caddy reverse proxy for WebSpec
After=network.target
Before=webspec-gateway.service

[Service]
Type=simple
User=${GATEWAY_USER}
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile
Restart=always
RestartSec=3

# Allow binding to port ${CADDY_PORT}
AmbientCapabilities=CAP_NET_BIND_SERVICE

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
echo "Created caddy-webspec.service"

# ── 7. Update gateway systemd service ──
GATEWAY_SERVICE="/etc/systemd/system/webspec-gateway.service"
if [ -f "$GATEWAY_SERVICE" ]; then
    # Add WEBSPEC_INTERNAL_PORT if not already present
    if ! grep -q "WEBSPEC_INTERNAL_PORT" "$GATEWAY_SERVICE"; then
        sed -i "/Environment=WEBSPEC_PORT=/a Environment=WEBSPEC_INTERNAL_PORT=${GATEWAY_PORT}" "$GATEWAY_SERVICE"
        echo "Added WEBSPEC_INTERNAL_PORT=${GATEWAY_PORT} to gateway service"
    fi
fi

# ── 8. Reload systemd and start services ──
systemctl daemon-reload
systemctl enable caddy-webspec.service

# Restart gateway with new port
systemctl restart webspec-gateway.service
echo "Gateway restarted on port ${GATEWAY_PORT}"

# Start Caddy
systemctl start caddy-webspec.service
echo "Caddy started on port ${CADDY_PORT}"

# ── 9. Verify ──
echo ""
echo "=== Verification ==="
sleep 2

# Check Caddy is running
if systemctl is-active --quiet caddy-webspec.service; then
    echo "  Caddy:   running"
else
    echo "  Caddy:   FAILED" >&2
fi

# Check gateway is running
if systemctl is-active --quiet webspec-gateway.service; then
    echo "  Gateway: running"
else
    echo "  Gateway: FAILED" >&2
fi

# Test catch-all (unknown subdomain → 421)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://unknown-test.localhost:${CADDY_PORT}/ 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "421" ]; then
    echo "  Catch-all: 421 (correct)"
else
    echo "  Catch-all: ${HTTP_CODE} (expected 421)" >&2
fi

echo ""
echo "Setup complete. Services available at *.${DOMAIN}"
echo ""
echo "Next steps:"
echo "  webspec-ctl ls              # list services"
echo "  webspec-ctl add <name> ...  # provision new service"
