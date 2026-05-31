#!/usr/bin/env python3
"""Generate a protocol list constant from IRremoteESP8266 SupportedProtocols.md.

Parses the vendor documentation and writes a Python set or list to stdout
that can be pasted into const.py. Not required for the first release —
the encoder dynamically discovers protocols from irhvac at runtime.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MD_PATH = REPO_ROOT / "vendor" / "IRremoteESP8266" / "SupportedProtocols.md"

HEADING_PATTERN = re.compile(r"^##\s+(.+)")


def main() -> None:
    if not MD_PATH.exists():
        print(f"Missing {MD_PATH}", file=sys.stderr)
        sys.exit(1)

    protocols: list[str] = []
    with open(MD_PATH, encoding="utf-8") as fh:
        for line in fh:
            m = HEADING_PATTERN.match(line)
            if m:
                name = m.group(1).strip()
                if name and not name.startswith("_"):
                    protocols.append(name)

    print("# Auto-generated from SupportedProtocols.md")
    print("SUPPORTED_PROTOCOLS: set[str] = {")
    for p in sorted(set(protocols)):
        print(f'    "{p}",')
    print("}")


if __name__ == "__main__":
    main()
