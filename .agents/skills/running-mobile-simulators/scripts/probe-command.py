#!/usr/bin/env python3

import argparse
import os
import signal
import subprocess
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one read-only command within a finite observation budget"
    )
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required")
    return args


def stop_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.communicate(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main() -> int:
    args = parse_args()
    try:
        process = subprocess.Popen(
            args.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 127
    try:
        stdout, stderr = process.communicate(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        stop_group(process)
        stdout, stderr = process.communicate()
        sys.stdout.buffer.write(stdout)
        sys.stderr.buffer.write(stderr)
        print("observation budget expired", file=sys.stderr)
        return 124
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
