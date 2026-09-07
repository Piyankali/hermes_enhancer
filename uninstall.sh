#!/usr/bin/env bash
# uninstall.sh - Remove ONLY the Hermes Enhancer plugin installation.
#
#   chmod +x uninstall.sh
#   ./uninstall.sh
#
# Removes:  $HERMES_HOME/plugins/hermes_enhancer
# Keeps:    everything else, including timestamped backups
#           (.hermes_enhancer.bak.*) so user-created files are never
#           destroyed silently.
#
# Never touches state.db, other plugins, Hermes itself, or user data.
# Idempotent: safe to run when nothing is installed.
#
# Environment overrides (used by tests):
#   HERMES_HOME - Hermes home directory (default: $HOME/.hermes)
set -euo pipefail

PLUGIN_ID="hermes_enhancer"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/$PLUGIN_ID"

if [ ! -d "$PLUGIN_DIR" ]; then
    echo "Hermes Enhancer is not installed ($PLUGIN_DIR absent)."
    echo "Status: NOTHING TO REMOVE"
    exit 0
fi

rm -rf "$PLUGIN_DIR"

if [ -d "$PLUGIN_DIR" ]; then
    echo "ERROR: failed to remove $PLUGIN_DIR" >&2
    exit 1
fi

echo "Removed Hermes Enhancer plugin ($PLUGIN_DIR)."
EXTRAS="$(ls -d "$HERMES_HOME"/plugins/.${PLUGIN_ID}.bak.* 2>/dev/null || true)"
if [ -n "$EXTRAS" ]; then
    echo "Kept install backups (remove manually if unwanted):"
    echo "$EXTRAS"
fi
echo "Status: UNINSTALLED"
echo "Note: ~/.hermes/state.db and all other plugins were left untouched."
