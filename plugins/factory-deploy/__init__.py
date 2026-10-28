"""Start auto-deploy for a push the github route kept, and run no agent turn for it.

The route keeps a push only for the factory repository's default branch, so this hook never reads the payload's
repository or ref. The job fetches the branch itself and trusts nothing in the payload. A hook that raises lets the
delivery reach a triage turn, so every push returns `skip`, whether or not the job started.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import subprocess
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

LABEL = "ai.patinaproject.factory.auto-deploy"
LAUNCHCTL = "/bin/launchctl"
START_TIMEOUT_SECONDS = 10.0
PUSH_KEYS = ("ref", "before", "after", "pusher")
SKIP = {"action": "skip", "reason": "a push to the factory's default branch starts auto-deploy, not a turn"}

Run = Callable[..., "subprocess.CompletedProcess[str]"]


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", make_dispatch_hook())


def is_push(payload: Any) -> bool:
    """A push payload has these keys and, unlike every other event the route keeps, no action."""
    return isinstance(payload, dict) and "action" not in payload and all(key in payload for key in PUSH_KEYS)


def domains(uid: int) -> tuple[str, ...]:
    # A LaunchAgent loads in the GUI domain when someone is logged in at the console, and in the user domain otherwise.
    return (f"gui/{uid}", f"user/{uid}")


def start_job(run: Run = subprocess.run, uid: Optional[int] = None) -> None:
    """Kickstart the job in the first domain that has it. Never raises; logs the outcome."""
    try:
        failures = []
        for domain in domains(os.getuid() if uid is None else uid):
            # The gateway's environment holds the factory's secrets, and launchctl needs none of them.
            proc = run(
                [LAUNCHCTL, "kickstart", f"{domain}/{LABEL}"],
                env={}, capture_output=True, text=True, timeout=START_TIMEOUT_SECONDS, check=False,
            )
            if proc.returncode == 0:
                logger.info("factory-deploy: started %s in %s", LABEL, domain)
                return
            failures.append(f"{domain} exited {proc.returncode}")
        logger.error("factory-deploy: could not start %s: %s", LABEL, "; ".join(failures))
    except Exception as failure:
        logger.error("factory-deploy: could not start %s: %s", LABEL, type(failure).__name__)


def make_dispatch_hook(
    start: Callable[[], None] = start_job,
    pool: Optional[concurrent.futures.Executor] = None,
) -> Callable[..., Optional[dict]]:
    # One worker: a burst of pushes kickstarts the job one at a time, and the job deploys whatever is newest.
    executor = pool or concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="factory-deploy")

    def skip_push(event: Any = None, **_: Any) -> Optional[dict]:
        try:
            source = event.source
            if source.platform.value != "webhook" or not is_push(event.raw_message):
                return None
        except Exception:
            return None
        try:
            executor.submit(start)
        except Exception as failure:
            logger.error("factory-deploy: could not schedule the start of %s: %s", LABEL, type(failure).__name__)
        return SKIP

    return skip_push
