#!/usr/bin/env python3
"""Generate a protocol list constant from IRremoteESP8266 SupportedProtocols.md.

Parses the vendor documentation table and writes a Python set to stdout
that can be used as a reference for protocol-based AC control.

Usage: python scripts/sync_protocol_list.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MD_PATH = REPO_ROOT / "vendor" / "IRremoteESP8266" / "SupportedProtocols.md"

# Table row pattern: | [Name](link) | **Brand** | ... | ... | [Yes/] |
# Group 1: protocol name (text inside [])
ROW_PATTERN = re.compile(
    r"^\|\s*\[([^\]]+)\]\(https://github\.com/crankyoldgit/IRremoteESP8266/blob/master/src/ir_\w+\.\w+\)"
)


def main() -> None:
    if not MD_PATH.exists():
        print(f"Missing {MD_PATH}", file=sys.stderr)
        sys.exit(1)

    protocols: set[str] = set()
    with open(MD_PATH, encoding="utf-8") as fh:
        for line in fh:
            m = ROW_PATTERN.match(line)
            if m:
                name = m.group(1).strip()
                if name and not name.startswith("_"):
                    protocols.add(name)

    if not protocols:
        print("No protocols found!", file=sys.stderr)
        sys.exit(1)

    print("# Auto-generated from vendor/IRremoteESP8266/SupportedProtocols.md")
    print("# Run: python scripts/sync_protocol_list.py")
    print("SUPPORTED_AC_PROTOCOLS: list[str] = [")
    for p in sorted(protocols):
        print(f'    "{p}",')
    print("]")
    print(f"\n# Total: {len(protocols)} protocols")


if __name__ == "__main__":
    main()
