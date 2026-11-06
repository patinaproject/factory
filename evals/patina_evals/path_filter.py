"""Decide whether a change touches files that qualify it for the eval. Stdlib only."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TABLE = Path(__file__).resolve().parents[1] / "qualifying_paths.json"


def glob_regex(pattern: str) -> re.Pattern[str]:
    """`**/` spans zero or more directories, `**` spans anything, `*` and `?` stay within a segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def load_table(path: Path = TABLE) -> dict:
    table = json.loads(path.read_text(encoding="utf-8"))
    return {key: [glob_regex(p) for p in table[key]] for key in ("include", "exclude")}


def qualifying(paths: list[str], table: dict | None = None) -> list[str]:
    table = table or load_table()
    return [
        path for path in paths
        if any(r.match(path) for r in table["include"]) and not any(r.match(path) for r in table["exclude"])
    ]


def changed_paths(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "-z", f"{base}...{head}"], check=True, capture_output=True, text=True
    )
    return [p for p in result.stdout.split("\0") if p]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report whether a change qualifies for the eval.")
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--paths-file", help="file with one changed path per line, or - for stdin")
    parser.add_argument("--table", default=str(TABLE))
    args = parser.parse_args(argv)
    if args.paths_file:
        text = sys.stdin.read() if args.paths_file == "-" else Path(args.paths_file).read_text(encoding="utf-8")
        paths = [line.strip() for line in text.splitlines() if line.strip()]
    elif args.base and args.head:
        paths = changed_paths(args.base, args.head)
    else:
        parser.error("pass --paths-file, or both --base and --head")
    matched = qualifying(paths, load_table(Path(args.table)))
    print(json.dumps({"qualifies": bool(matched), "matched": matched}))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"qualifies={'true' if matched else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
