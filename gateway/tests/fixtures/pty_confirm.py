"""Drive approve_cli._confirm through a real pseudo-terminal (run as a separate process).

Exit 0 iff the child, whose controlling terminal is the pty, accepted "approve".
"""
import os
import pty
import select
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from webspec import approve_cli  # noqa: E402  (import before fork)

pid, fd = pty.fork()
if pid == 0:
    try:
        os._exit(0 if approve_cli._confirm("Type 'approve': ") else 3)
    except BaseException:
        os._exit(4)

deadline = time.time() + 10
out = b""
while b"approve'" not in out and time.time() < deadline:
    if select.select([fd], [], [], 1)[0]:
        out += os.read(fd, 1024)
if b"approve'" not in out:
    os.kill(pid, signal.SIGKILL)
    sys.exit(10)
os.write(fd, b"approve\n")
while time.time() < deadline:
    done, status = os.waitpid(pid, os.WNOHANG)
    if done:
        sys.exit(os.waitstatus_to_exitcode(status))
    if select.select([fd], [], [], 0.2)[0]:
        try:
            os.read(fd, 1024)  # drain the echo so the child never blocks
        except OSError:
            pass
os.kill(pid, signal.SIGKILL)
sys.exit(11)
