"""The active Xcode and whether its first launch is done.

check-install and claude-session share this. A full Xcode update leaves a first launch pending until
`sudo xcodebuild -runFirstLaunch` runs, and `xcrun` then fails or hangs until it does. A check that outlives
its limit is a timeout, reported apart from a pending first launch because host load slows it too.
"""
from __future__ import annotations

import enum
import subprocess
from pathlib import Path
from typing import Mapping, NamedTuple, Optional

FIRST_LAUNCH_SECONDS = 30
DEVELOPER_DIR_SECONDS = 10
FIRST_LAUNCH_FIX = "sudo xcodebuild -runFirstLaunch"


def developer_dir(env: Mapping[str, str]) -> Optional[Path]:
    """The full Xcode's developer directory, or None for the Command Line Tools alone or no developer directory."""
    chosen = env.get("DEVELOPER_DIR")
    if not chosen:
        try:
            chosen = subprocess.run(
                ["xcode-select", "-p"], capture_output=True, text=True, timeout=DEVELOPER_DIR_SECONDS
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
    path = Path(chosen) if chosen else None
    if path is None or not path.is_dir() or not any(part.endswith(".app") for part in path.parts):
        return None
    return path


class FirstLaunch(enum.Enum):
    DONE = "done"
    PENDING = "pending"
    TIMED_OUT = "timed out"


class FirstLaunchStatus(NamedTuple):
    state: FirstLaunch
    developer: Optional[Path]  # None when the state is DONE


def first_launch_status(xcodebuild: str, env: Mapping[str, str]) -> FirstLaunchStatus:
    """DONE for a finished first launch or no full Xcode, PENDING when `-checkFirstLaunchStatus` exits non-zero,
    TIMED_OUT when it outlives FIRST_LAUNCH_SECONDS. The last two carry the developer directory."""
    developer = developer_dir(env)
    if developer is None:
        return FirstLaunchStatus(FirstLaunch.DONE, None)
    try:
        done = subprocess.run(
            [xcodebuild, "-checkFirstLaunchStatus"], capture_output=True, timeout=FIRST_LAUNCH_SECONDS,
            env={**env, "DEVELOPER_DIR": str(developer)},
        ).returncode == 0
    except subprocess.TimeoutExpired:
        return FirstLaunchStatus(FirstLaunch.TIMED_OUT, developer)
    return FirstLaunchStatus(FirstLaunch.DONE, None) if done else FirstLaunchStatus(FirstLaunch.PENDING, developer)


def timeout_message() -> str:
    return (
        f"xcodebuild -checkFirstLaunchStatus timed out after {FIRST_LAUNCH_SECONDS:g} seconds; "
        "host load is a likely cause"
    )
