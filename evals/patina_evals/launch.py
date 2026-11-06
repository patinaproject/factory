"""Start one trial command in its own process group under resource ceilings. Stdlib only."""

from __future__ import annotations

import argparse
import os
import resource
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu-seconds", type=int, required=True)
    parser.add_argument("--memory-bytes", type=int, required=True)
    parser.add_argument("--pgids", required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    os.setsid()
    with open(args.pgids, "a", encoding="utf-8") as handle:
        handle.write(f"{os.getpgrp()}\n")
    resource.setrlimit(resource.RLIMIT_CPU, (args.cpu_seconds, args.cpu_seconds))
    # macOS accepts RLIMIT_AS but does not enforce it, so the trial records the ceiling as unenforced there.
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (args.memory_bytes, args.memory_bytes))
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
