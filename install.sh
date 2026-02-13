#!/usr/bin/env bash
set -euo pipefail

PROXY_HOST="${PROXY_HOST:-localhost}"
PROXY_PORT="${PROXY_PORT:-8080}"
PROXY_URL="http://${PROXY_HOST}:${PROXY_PORT}"

echo "=== Claude Code Proxy Installer ==="
echo "Proxy URL: ${PROXY_URL}"

# Detect shell RC file
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "$SHELL")" = "zsh" ]; then
    RC_FILE="${HOME}/.zshrc"
elif [ -n "${BASH_VERSION:-}" ] || [ "$(basename "$SHELL")" = "bash" ]; then
    RC_FILE="${HOME}/.bashrc"
else
    RC_FILE="${HOME}/.profile"
fi

echo "Shell config: ${RC_FILE}"

EXPORT_LINE="export ANTHROPIC_BASE_URL=\"${PROXY_URL}\""

# Idempotent: remove old entry, then add new one
if [ -f "$RC_FILE" ]; then
    # Cross-platform sed in-place
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' '/^export ANTHROPIC_BASE_URL=/d' "$RC_FILE"
    else
        sed -i '/^export ANTHROPIC_BASE_URL=/d' "$RC_FILE"
    fi
fi

echo "$EXPORT_LINE" >> "$RC_FILE"
echo "Added to ${RC_FILE}: ${EXPORT_LINE}"

# Also export for current session
export ANTHROPIC_BASE_URL="${PROXY_URL}"

# Health check if proxy is reachable
echo ""
echo "Checking proxy health..."
if curl -sf "${PROXY_URL}/health" > /dev/null 2>&1; then
    echo "Proxy is running and healthy!"
else
    echo "Proxy is not reachable at ${PROXY_URL} (this is OK if you haven't started it yet)."
fi

echo ""
echo "Done! Run 'source ${RC_FILE}' or open a new terminal to activate."
echo "Claude Code will now route through the proxy at ${PROXY_URL}"
