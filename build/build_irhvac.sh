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

# Only auto-detect musl suffix when the caller did NOT pass an explicit output_dir.
# CI workflows pass ${{ matrix.out_dir }} explicitly, so we don't tweak it.
if [[ $# -eq 1 ]]; then
    if ldd /bin/ls 2>/dev/null | grep -q musl; then
        OUTPUT_DIR="${OUTPUT_DIR}_musl"
        echo "Detected musl libc → output: $OUTPUT_DIR"
    fi
fi

# Replace the SWIG interface file with our patched version.
# The upstream uses %module (package="pyhvac") irhvac, which causes SWIG
# to generate PyInit_pyhvac_irhvac instead of PyInit_irhvac.  Python's
# extension loader will reject the .so if the init function name does not
# match the bare module name we request ("irhvac").
PATCHED_IF="$REPO_ROOT/build/patches/libirhvac.i"
SWIG_IF="$PYTHON_DIR/libirhvac.i"
if [ -f "$PATCHED_IF" ]; then
    cp "$PATCHED_IF" "$SWIG_IF"
    echo "=== Patched SWIG .i (will produce PyInit_irhvac) ==="
    grep '^%module' "$SWIG_IF"
else
    echo "WARNING: $PATCHED_IF not found; SWIG module name may be wrong"
fi

cd "$PYTHON_DIR"

# Clean previous build artifacts.
make distclean 2>/dev/null || true

# Build _irhvac.so directly.
# Its dependency chain (libirhvac_wrap.cxx) invokes the system `swig`
# command. We skip the `swig` target because it contains a Docker-detection
# block that looks for ./swig-4.2.0/swig which does not exist in our image.
make _irhvac.so

# Verify the .so exports a usable PyInit symbol.
# SWIG may produce PyInit_irhvac OR PyInit__irhvac (latter = upstream package=pyhvac).
# The loader handles both, so accept either.
EXPORTS=$(nm -D _irhvac.so 2>/dev/null | grep ' PyInit' || objdump -T _irhvac.so 2>/dev/null | grep ' PyInit' || true)
if echo "$EXPORTS" | grep -qE 'PyInit_(_?irhvac)'; then
    echo "=== Verified: _irhvac.so exports a usable PyInit ==="
    echo "$EXPORTS"
else
    echo "=== ERROR: _irhvac.so does NOT export PyInit_irhvac or PyInit__irhvac ==="
    echo "$EXPORTS"
    exit 1
fi

# Copy output.
mkdir -p "$REPO_ROOT/$OUTPUT_DIR"
cp _irhvac.so irhvac.py "$REPO_ROOT/$OUTPUT_DIR/"

echo "Built _irhvac.so → $OUTPUT_DIR"
