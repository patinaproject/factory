"""The active Xcode and whether its first launch is done.

check-install and claude-session share this. A full Xcode update leaves a first launch pending until
`sudo xcodebuild -runFirstLaunch` runs, and `xcrun` then fails or hangs until it does.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping, Optional

FIRST_LAUNCH_SECONDS = 10
FIRST_LAUNCH_FIX = "sudo xcodebuild -runFirstLaunch"


def developer_dir(env: Mapping[str, str]) -> Optional[Path]:
    """The full Xcode's developer directory, or None for the Command Line Tools alone or no developer directory."""
    chosen = env.get("DEVELOPER_DIR")
    if not chosen:
        try:
            chosen = subprocess.run(
                ["xcode-select", "-p"], capture_output=True, text=True, timeout=FIRST_LAUNCH_SECONDS
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
    path = Path(chosen) if chosen else None
    if path is None or not path.is_dir() or not any(part.endswith(".app") for part in path.parts):
        return None
    return path


def first_launch_pending(xcodebuild: str, env: Mapping[str, str]) -> Optional[Path]:
    """The developer directory of a full Xcode whose first launch is pending, else None. A hung check is pending."""
    developer = developer_dir(env)
    if developer is None:
        return None
    try:
        done = subprocess.run(
            [xcodebuild, "-checkFirstLaunchStatus"], capture_output=True, timeout=FIRST_LAUNCH_SECONDS,
            env={**env, "DEVELOPER_DIR": str(developer)},
        ).returncode == 0
    except subprocess.TimeoutExpired:
        done = False
    return None if done else developer
