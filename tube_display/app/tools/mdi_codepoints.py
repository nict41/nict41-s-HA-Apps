#!/usr/bin/env python3
"""Build the Material Design Icons name -> codepoint map.

Parses the .mdi-name:before { content: "\fXXXX"; } rules out of the official
materialdesignicons.css and writes them as a JSON object. Run at image build
time (see Dockerfile); the app loads the JSON at startup, so rendering never
needs the network.
"""

import json
import re
import sys

_RULE = re.compile(r"\.mdi-([a-z0-9-]+)::?before\s*\{\s*content:\s*\"\\([0-9a-fA-F]{4,6})\"")


def build(css_path: str) -> dict:
    with open(css_path, encoding="utf-8") as f:
        css = f.read()
    return {name: int(cp, 16) for name, cp in _RULE.findall(css)}


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <materialdesignicons.css> <out.json>", file=sys.stderr)
        return 2
    codepoints = build(sys.argv[1])
    if len(codepoints) < 1000:
        print(f"suspiciously few icons parsed: {len(codepoints)}", file=sys.stderr)
        return 1
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(codepoints, f, ensure_ascii=False, sort_keys=True)
    print(f"wrote {len(codepoints)} codepoints to {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
