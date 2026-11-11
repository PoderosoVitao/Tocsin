from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

Result = Literal["accepted", "rejected", "unreachable"]


@dataclass(frozen=True)
class Sent:
    result: Result
    # What the server said, or what went wrong reaching it.
    message: str


def _post(url: str, body: bytes, timeout: float, attempts: int) -> Sent:
    message = ""
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return Sent("accepted", response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            # A refusal (a report the check can't read, an unknown check) won't
            # change on a retry; a busy or failing server might.
            if exc.code < 500 and exc.code != 429:
                return Sent("rejected", f"{exc.code}: {text}")
            message = f"HTTP {exc.code}"
        except Exception as exc:
            message = str(exc) or type(exc).__name__
        if attempt < attempts:
            time.sleep(attempt)
    return Sent("unreachable", message)


# Posts a report as the body of a check's success ping.
def send_report(ping_url: str, report: dict[str, Any], timeout: float = 10) -> Sent:
    return _post(ping_url.rstrip("/"), json.dumps(report).encode(), timeout, 3)


# Reports that the job failed, with the reason as the output, for when there is
# no report to send: the check goes down with the reason in its alert instead
# of the job looking like it never ran.
def send_failure(ping_url: str, message: str, timeout: float = 10) -> Sent:
    return _post(f"{ping_url.rstrip('/')}/fail", message.encode(), timeout, 3)
