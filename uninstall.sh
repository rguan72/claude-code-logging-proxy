#!/usr/bin/env bash
set -euo pipefail

echo "=== Claude Code Proxy Uninstaller ==="
echo ""

# Detect shell RC file (override with RC_FILE env var)
if [ -n "${RC_FILE:-}" ]; then
    : # use caller-provided RC_FILE
elif [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "$SHELL")" = "zsh" ]; then
    RC_FILE="${HOME}/.zshrc"
elif [ -n "${BASH_VERSION:-}" ] || [ "$(basename "$SHELL")" = "bash" ]; then
    RC_FILE="${HOME}/.bashrc"
else
    RC_FILE="${HOME}/.profile"
fi

echo "Shell config: ${RC_FILE}"

if [ ! -f "$RC_FILE" ]; then
    echo "File not found: ${RC_FILE}"
    exit 1
fi

# Remove proxy alias and export lines
if grep -q 'ANTHROPIC_BASE_URL=.*claude' "$RC_FILE" || grep -q '^export ANTHROPIC_BASE_URL=' "$RC_FILE"; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' '/ANTHROPIC_BASE_URL=.*claude/d' "$RC_FILE"
        sed -i '' '/^export ANTHROPIC_BASE_URL=/d' "$RC_FILE"
    else
        sed -i '/ANTHROPIC_BASE_URL=.*claude/d' "$RC_FILE"
        sed -i '/^export ANTHROPIC_BASE_URL=/d' "$RC_FILE"
    fi
    echo "Removed proxy configuration from ${RC_FILE}."
else
    echo "No proxy configuration found in ${RC_FILE}."
fi

# Clear from current session
unset ANTHROPIC_BASE_URL 2>/dev/null || true
unalias claude 2>/dev/null || true

echo ""
echo "Done! Run 'source ${RC_FILE}' or open a new terminal to finish."
