#!/usr/bin/env bash
set -euo pipefail

PROXY_URL="${PROXY_URL:-http://18.224.228.178:8080}"

echo "=== Claude Code Proxy Installer ==="
echo "Proxy URL: ${PROXY_URL}"
echo ""

# Check if claude is installed
if ! command -v claude &> /dev/null; then
    echo "Error: 'claude' command not found. Install Claude Code first:"
    echo "  npm install -g @anthropic-ai/claude-code"
    exit 1
fi

# Detect shell RC file
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "$SHELL")" = "zsh" ]; then
    RC_FILE="${HOME}/.zshrc"
elif [ -n "${BASH_VERSION:-}" ] || [ "$(basename "$SHELL")" = "bash" ]; then
    RC_FILE="${HOME}/.bashrc"
else
    RC_FILE="${HOME}/.profile"
fi

echo "Shell config: ${RC_FILE}"

ALIAS_LINE="alias claude='ANTHROPIC_BASE_URL=${PROXY_URL} claude'"

# Idempotent: remove old proxy alias/export, then add new one
if [ -f "$RC_FILE" ]; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' '/ANTHROPIC_BASE_URL=.*claude/d' "$RC_FILE"
        sed -i '' '/^export ANTHROPIC_BASE_URL=/d' "$RC_FILE"
    else
        sed -i '/ANTHROPIC_BASE_URL=.*claude/d' "$RC_FILE"
        sed -i '/^export ANTHROPIC_BASE_URL=/d' "$RC_FILE"
    fi
fi

echo "$ALIAS_LINE" >> "$RC_FILE"
echo "Added to ${RC_FILE}:"
echo "  ${ALIAS_LINE}"

# Health check
echo ""
echo "Checking proxy..."
if curl -sf --connect-timeout 5 "${PROXY_URL}/health" > /dev/null 2>&1; then
    echo "Proxy is reachable!"
else
    echo "Warning: Proxy not reachable at ${PROXY_URL} (it may be starting up)."
fi

echo ""
echo "Done! Run 'source ${RC_FILE}' or open a new terminal, then use 'claude' as normal."
