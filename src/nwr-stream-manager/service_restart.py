"""Restarting NWR Stream Manager from the page: it shuts down as it does on exit, then starts again."""

from __future__ import annotations

import logging
import os
import shlex
import sys
from typing import Any, Callable


LOG = logging.getLogger(__name__)

RESTART_RESPONSE_SECONDS = 0.5

ALREADY_RESTARTED_MESSAGE = (
    "NWR Stream Manager has already restarted since this page was opened. Reload the page, then restart it again if you still need to."
)


class RestartServiceMixin:
    """Mixed into RtlControlService."""

    # Set by run_server once the web server exists; stops it from another thread.
    stop_web_server: Callable[[], None] | None = None

    def request_restart(self, payload: dict[str, Any], account_name: str = "") -> dict[str, Any]:
        """Shuts down for a restart, once, and only for a request made to this run of it.

        A restart request names the run it was made to. A form sent again after the
        restart, or from a page left open from before it, names a run that has
        ended, so it is refused rather than restarting again.
        """
        if not isinstance(payload, dict) or str(payload.get("instance", "")) != self.instance_id:
            raise ValueError(ALREADY_RESTARTED_MESSAGE)
        with self.lock:
            if self.restart_requested.is_set() or self.shutting_down.is_set():
                raise ValueError("NWR Stream Manager is already restarting or shutting down.")
            if self.stop_web_server is None:
                raise ValueError("NWR Stream Manager can't restart itself here.")
            self.restart_requested.set()
        LOG.info("restart requested by %s", account_name or "an unknown account")
        self.begin_shutdown()
        self.stop_web_server()
        return {"restarting": True, "instance": self.instance_id}


def restart_command() -> list[str]:
    """The command this process was started with, run by the same Python."""
    return [sys.executable, *sys.orig_argv[1:]]


def restart_process() -> int:
    """Replaces this process with a fresh start of the same command.

    The new run takes over this process rather than being started beside it, so
    there is never more than one, and it loads the code as it is now on disk.
    Returns only if that fails.
    """
    command = restart_command()
    LOG.info("restarting NWR Stream Manager: %s", shlex.join(command))
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(command[0], command)
    except OSError as exc:
        print(f"Error: NWR Stream Manager couldn't restart itself: {exc}", file=sys.stderr)
    return 1
