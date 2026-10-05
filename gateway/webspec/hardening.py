"""Process hardening applied at gateway startup.

On Linux, mark the process non-dumpable (``prctl(PR_SET_DUMPABLE, 0)``). This makes
``/proc/<pid>/environ``, ``/proc/<pid>/mem`` and ptrace unavailable to other processes
of the same user — including the stdio MCP servers the gateway spawns and any agent
shell running as the same user — so the guard key in the gateway's environment can't
be read that way. (Credential theft from ``/proc/*/environ`` and process memory is a
documented 2026 agent behavior.) It is the closest Linux analog of macOS's Hardened
Runtime refusing task-port/debugger access.

It is defense in depth, not a substitute for running the gateway as its own OS user:
on macOS, same-user processes can still read another process's environment via
``sysctl(KERN_PROCARGS2)``, so a dedicated user is the real boundary there.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger("webspec.hardening")

PR_SET_DUMPABLE = 4


def harden_process() -> bool:
    """Best-effort; returns True if the process was made non-dumpable."""
    if not sys.platform.startswith("linux"):
        logger.info("Process hardening: non-dumpable is Linux-only; run the gateway as a dedicated OS user.")
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            logger.warning("prctl(PR_SET_DUMPABLE, 0) failed: errno %d", ctypes.get_errno())
            return False
    except (OSError, AttributeError) as e:
        logger.warning("Process hardening unavailable: %s", e)
        return False
    logger.info("Process hardening: non-dumpable (environ/mem/ptrace closed to same-user processes).")
    return True
