#!/usr/bin/env bash
# Copy compiled native modules into custom_components/hair/native/.
# Used after CI builds to assemble the release tree.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST="$REPO_ROOT/custom_components/hair/native"
echo "Packaging native modules into $DEST"
ls -la "$DEST"/linux_*/_irhvac.so 2>/dev/null || echo "No .so files found — run build_irhvac.sh first."
