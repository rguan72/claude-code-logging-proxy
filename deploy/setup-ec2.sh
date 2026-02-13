#!/usr/bin/env bash
set -euo pipefail

echo "=== Claude Code Proxy - EC2 Setup ==="

# Install system dependencies
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

# Create app directory
APP_DIR="/opt/claude-code-proxy"
sudo mkdir -p "$APP_DIR"
sudo chown "$(whoami):$(whoami)" "$APP_DIR"

# Copy files
cp config.py logger.py proxy.py requirements.txt "$APP_DIR/"

# Create venv and install deps
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Create log directory
mkdir -p "$APP_DIR/logs"

# Create systemd service
sudo tee /etc/systemd/system/claude-proxy.service > /dev/null <<EOF
[Unit]
Description=Claude Code Proxy
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$APP_DIR
Environment=LOG_DIR=$APP_DIR/logs
ExecStart=$APP_DIR/venv/bin/uvicorn proxy:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable claude-proxy
sudo systemctl start claude-proxy

echo ""
echo "Claude Code Proxy is running on port 8080"
echo "Check status: sudo systemctl status claude-proxy"
echo "View logs:    sudo journalctl -u claude-proxy -f"
