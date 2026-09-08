#!/usr/bin/env bash
# setup.sh - Hermes Enhancer v0.22.2 plugin installer.
#
# Installs Hermes Enhancer into the Hermes user plugin directory without
# requiring manual file copies or configuration edits.
#
#   chmod +x setup.sh
#   ./setup.sh
#
# Idempotent: safe to run repeatedly. Existing installations are backed up
# (timestamped) before replacement, never merged.
#
# Safety: never touches ~/.hermes/state.db (or $HERMES_HOME/state.db),
# never runs `hermes doctor --fix`, never uses sudo, never touches the
# network or shell startup files.
#
# Environment overrides (used by tests):
#   HERMES_HOME - Hermes home directory (default: $HOME/.hermes)
#   SKIP_DISCOVERY - set to 1 to skip the `hermes plugins list` check
#                    (e.g. Hermes CLI not installed)
set -euo pipefail

EXPECTED_VERSION="0.23.0"
PLUGIN_ID="hermes_enhancer"

# [1/6] Locate repository root (directory containing this script).
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/$PLUGIN_ID"

echo "Hermes Enhancer v$EXPECTED_VERSION"
echo "======================="
echo ""

# [1/6] Checking project files.
echo "[1/6] Checking project..."
REQUIRED_FILES="plugin.yaml README.md src/__init__.py src/enhancer.py src/federated_db.py src/feedback_optimizer.py src/predictive_preload.py src/meta_learner.py src/skill_graph.py src/composer.py src/self_test.py src/redaction.py src/decision_engine.py"
for f in $REQUIRED_FILES; do
    if [ ! -f "$ROOT_DIR/$f" ]; then
        echo "ERROR: required file missing: $f" >&2
        exit 1
    fi
done
INSTALLED_VERSION="$(grep -E '^version:' "${ROOT_DIR}/plugin.yaml" | head -1 | sed 's/^version: *//')"
if [ "$INSTALLED_VERSION" != "$EXPECTED_VERSION" ]; then
    echo "ERROR: plugin version mismatch: plugin.yaml says '$INSTALLED_VERSION', expected '$EXPECTED_VERSION'" >&2
    exit 1
fi
echo "      project OK (version $INSTALLED_VERSION)"

# [2/6] Detecting Hermes plugin directory.
echo "[2/6] Detecting Hermes..."
if [ -z "${HERMES_HOME:-}" ]; then
    echo "ERROR: Hermes plugin directory could not be determined (HOME unset and HERMES_HOME unset)" >&2
    exit 1
fi
echo "      plugin directory: $PLUGIN_DIR"

# [3/6] Preparing staging area with exactly the runtime file set.
echo "[3/6] Preparing plugin directory..."
STAGE="$(mktemp -d "$HERMES_HOME/.${PLUGIN_ID}.stage.XXXXXX" 2>/dev/null || mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
for mod in __init__ enhancer federated_db feedback_optimizer predictive_preload meta_learner skill_graph composer self_test redaction decision_engine; do
    cp "$ROOT_DIR/src/$mod.py" "$STAGE/$mod.py"
done
cp "$ROOT_DIR/plugin.yaml" "$STAGE/plugin.yaml"
cp "$ROOT_DIR/README.md" "$STAGE/README.md"
chmod 644 "$STAGE"/*.py "$STAGE/plugin.yaml" "$STAGE/README.md"
STAGE_VERSION="$(grep -E '^version:' "${STAGE}/plugin.yaml" | head -1 | sed 's/^version: *//')"
if [ "$STAGE_VERSION" != "$EXPECTED_VERSION" ]; then
    echo "ERROR: staged plugin version mismatch: '$STAGE_VERSION'" >&2
    exit 1
fi

# [4/6] Installing (atomic replace; previous install backed up, never merged).
echo "[4/6] Installing plugin..."
mkdir -p "$HERMES_HOME/plugins"
if [ -d "$PLUGIN_DIR" ]; then
    BACKUP="$HERMES_HOME/plugins/.${PLUGIN_ID}.bak.$(date +%Y%m%d_%H%M%S)"
    echo "      existing installation found -> backing up to $BACKUP"
    mv "$PLUGIN_DIR" "$BACKUP"
    echo "      (Already installed: updating safely)"
else
    echo "      fresh installation"
fi
mv "$STAGE" "$PLUGIN_DIR"
trap - EXIT

# [5/6] Verifying installation from disk (never from memory).
echo "[5/6] Verifying installation..."
for mod in __init__ enhancer federated_db feedback_optimizer predictive_preload meta_learner skill_graph composer self_test redaction decision_engine; do
    if [ ! -f "$PLUGIN_DIR/$mod.py" ]; then
        echo "ERROR: installed file missing: $mod.py" >&2
        exit 1
    fi
done
if [ ! -f "$PLUGIN_DIR/plugin.yaml" ]; then
    echo "ERROR: installed plugin.yaml missing" >&2
    exit 1
fi
FINAL_VERSION="$(grep -E '^version:' "${PLUGIN_DIR}/plugin.yaml" | head -1 | sed 's/^version: *//')"
if [ "$FINAL_VERSION" != "$EXPECTED_VERSION" ]; then
    echo "ERROR: installed version mismatch: '$FINAL_VERSION'" >&2
    exit 1
fi
VDIR="$(mktemp -d)"
cp "$PLUGIN_DIR/federated_db.py" "$PLUGIN_DIR/enhancer.py" "$VDIR/"
python3 -m py_compile "$VDIR/federated_db.py" "$VDIR/enhancer.py" || {
    echo "ERROR: installed Python files do not compile" >&2
    rm -rf "$VDIR"
    exit 1
}
rm -rf "$VDIR"

# [6/6] Plugin discovery via the real Hermes CLI (read-only).
echo "[6/6] Complete"
DISCOVERY="SKIPPED (Hermes CLI not available)"
if [ "${SKIP_DISCOVERY:-0}" != "1" ] && command -v hermes >/dev/null 2>&1; then
    DISCOVERY="WARNING (not listed yet; restart Hermes Agent or run: hermes plugins list)"
    # NOTE: --plain avoids Rich table line-wrapping under pipes, which
    # made name matching flaky. The plugin index can lag directory
    # changes; retry up to ~50s.
    for _try in 1 2 3 4 5; do
        if hermes plugins list --plain 2>/dev/null | grep -q "$PLUGIN_ID"; then
            DISCOVERY="PASS"
            break
        fi
        sleep 10
    done
fi

echo ""
echo "Plugin : Hermes Enhancer"
echo "Version: $FINAL_VERSION"
echo "Status : INSTALLED ($PLUGIN_DIR)"
echo "Plugin installation: PASS"
echo "Plugin discovery: $DISCOVERY"
echo "Plugin version: $FINAL_VERSION"
