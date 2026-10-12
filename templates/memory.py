#!/usr/bin/env python3
"""Install the factory's entries in the memory file of Hermes' own agent.

Renders `hermes-memory.txt` with the GitHub App login from the factory settings
and the worker's gh directory. Each entry starts with a label, such as
`GitHub:`. An existing entry with the same label is replaced, and every other
entry is kept, so the agent's own memories survive. Hermes separates entries
with a line holding only `§` and limits the file's size.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import render

TEMPLATE = render.TEMPLATE_DIR / "hermes-memory.txt"
SEPARATOR = "\n§\n"
# Hermes' default memory.memory_char_limit.
CHAR_LIMIT = 2200


def split_entries(text: str) -> list:
    return [entry.strip() for entry in text.split(SEPARATOR) if entry.strip()]


def label(entry: str) -> str:
    return entry.split(":", 1)[0]


def factory_entries(github_login: str, gh_config_dir: str) -> list:
    text = render.render_prompt(
        TEMPLATE.read_text(encoding="utf-8"), {"github_login": github_login, "gh_config_dir": gh_config_dir}
    )
    return split_entries(text)


def merge(existing: list, entries: list) -> list:
    by_label = {label(entry): entry for entry in entries}
    merged = [by_label.pop(label(entry), entry) for entry in existing]
    return merged + [entry for entry in entries if label(entry) in by_label]


def problems(existing: list, entries: list) -> list:
    current = {label(entry): entry for entry in existing}
    return [
        f"memory entry {label(entry)}: {'missing' if label(entry) not in current else 'differs from the template'}"
        for entry in entries
        if current.get(label(entry)) != entry
    ]


def write(path: Path, entries: list) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(SEPARATOR.join(entries))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("memory_file", type=Path, help="the memory file, <home>/memories/MEMORY.md")
    parser.add_argument("--gh-config-dir", required=True, help="the worker's gh directory, <worker>/gh")
    parser.add_argument("--settings-json", help="read settings from this JSON file instead of `hermes config get`")
    parser.add_argument("--check", action="store_true", help="report missing or different entries without writing")
    args = parser.parse_args(argv)
    try:
        settings = render.validate(render.load_settings(args.settings_json))
    except render.SettingsError as exc:
        print(f"memory.py: invalid settings: {exc}", file=sys.stderr)
        return 2
    if settings["github_login"] is None:
        print("memory.py: invalid settings: github.login is empty", file=sys.stderr)
        return 2
    entries = factory_entries(settings["github_login"], args.gh_config_dir)
    existing = split_entries(args.memory_file.read_text(encoding="utf-8")) if args.memory_file.exists() else []
    if args.check:
        found = problems(existing, entries)
        for line in found:
            print(line)
        return 1 if found else 0
    merged = merge(existing, entries)
    size = len(SEPARATOR.join(merged))
    if size > CHAR_LIMIT:
        print(f"memory.py: the merged memory has {size} characters, over Hermes' limit of {CHAR_LIMIT}; "
              f"remove entries from {args.memory_file} and run again", file=sys.stderr)
        return 1
    if merged != existing:
        write(args.memory_file, merged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
