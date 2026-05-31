#!/usr/bin/env bash
# Build _irhvac.so for the given architecture.
# Usage: ./build/build_irhvac.sh <x86_64|aarch64> [output_dir]
#
# Prerequisites: swig (4.2.x), g++, python3-dev, make
# Run from the repository root.
set -euo pipefail

ARCH="${1:?Usage: build_irhvac.sh <x86_64|aarch64> [output_dir]}"
OUTPUT_DIR="${2:-custom_components/hair/native/linux_${ARCH}}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_DIR="$REPO_ROOT/vendor/IRremoteESP8266/python"

cd "$PYTHON_DIR"

# Clean previous build artifacts.
make distclean 2>/dev/null || true

# Build _irhvac.so directly.
# Its dependency chain (libirhvac_wrap.cxx) invokes the system `swig`
# command. We skip the `swig` target because it contains a Docker-detection
# block that looks for ./swig-4.2.0/swig which does not exist in our image.
make _irhvac.so

# Copy output.
mkdir -p "$REPO_ROOT/$OUTPUT_DIR"
cp _irhvac.so irhvac.py "$REPO_ROOT/$OUTPUT_DIR/"

echo "Built _irhvac.so for linux_${ARCH} → $OUTPUT_DIR"
