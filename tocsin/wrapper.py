from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Sequence
from types import FrameType
from typing import IO

# Standard library only: this runs on the machines the jobs run on, from the
# dependency-free base install, and must never be the reason a job fails.

OUTPUT_TAIL_BYTES = 10_000
FINISH_ATTEMPTS = 3


def _warn(message: str) -> None:
    print(f"tocsin: {message}", file=sys.stderr, flush=True)


# Sends one ping, retrying network and server errors a few times. Never
# raises: a monitor that can't be reached is reported on stderr and ignored.
def _ping(url: str, body: bytes | None, timeout: float, attempts: int, what: str) -> None:
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body, method="POST" if body else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout):
                return
        except urllib.error.HTTPError as exc:
            # A 4xx (unknown check, bad signal) won't change on a retry.
            if exc.code < 500 and exc.code != 429:
                _warn(f"ping refused ({exc.code}): {url}")
                return
            error = f"HTTP {exc.code}"
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        if attempt < attempts:
            time.sleep(attempt)
    _warn(f"could not reach tocsin ({error}); {what} was not reported")


# Copies a child's output stream to our own as it arrives, keeping the last
# lines for the finish ping.
def _tee(source: IO[bytes], sink: IO[bytes], tail: deque[bytes], lock: threading.Lock) -> None:
    for line in iter(source.readline, b""):
        sink.write(line)
        sink.flush()
        with lock:
            tail.append(line)
    source.close()


# The exit status a shell would report: the code itself, or 128 plus the
# signal number for a process killed by a signal.
def _exit_status(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 - returncode


# Runs the command between a start ping and a finish ping that carries its
# exit status and the last lines of its output, and returns the command's own
# exit status.
#
# The command runs whatever happens to tocsin: pings that fail are reported on
# stderr and skipped. SIGTERM and SIGHUP are passed on to the command (a
# service manager stopping the job sends them to this process only). SIGINT is
# not, since a terminal already sends Ctrl-C to the whole process group.
def run(
    command: Sequence[str],
    ping_url: str,
    tail_lines: int = 100,
    timeout: float = 10,
) -> int:
    ping_url = ping_url.rstrip("/")
    _ping(f"{ping_url}/start", None, timeout, 1, "the job's start")

    tail: deque[bytes] = deque(maxlen=tail_lines)
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        status = 127 if isinstance(exc, FileNotFoundError) else 126
        message = f"could not start {command[0]}: {exc.strerror or exc}"
        _warn(message)
        _ping(f"{ping_url}/{status}", message.encode(), timeout, FINISH_ATTEMPTS, "the failure")
        return status

    def forward(signum: int, frame: FrameType | None) -> None:
        process.send_signal(signum)

    previous = {
        signum: signal.signal(signum, handler)
        for signum, handler in (
            (signal.SIGTERM, forward),
            (signal.SIGHUP, forward),
            (signal.SIGINT, signal.SIG_IGN),
        )
    }
    lock = threading.Lock()
    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=_tee, args=(process.stdout, sys.stdout.buffer, tail, lock)),
        threading.Thread(target=_tee, args=(process.stderr, sys.stderr.buffer, tail, lock)),
    ]
    try:
        for reader in readers:
            reader.start()
        returncode = process.wait()
        for reader in readers:
            reader.join()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    status = _exit_status(returncode)
    body = b"".join(tail)[-OUTPUT_TAIL_BYTES:]
    _ping(f"{ping_url}/{status}", body or None, timeout, FINISH_ATTEMPTS, "the job's result")
    return status
