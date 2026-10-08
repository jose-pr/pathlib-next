"""Waiting for a condition, bounded."""

from __future__ import annotations

import time


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.02) -> bool:
    """Poll `predicate` until it is true or `timeout` seconds have passed;
    returns its last value. A test asserts the result: what must be true
    after the wait, not how long it took to get there."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
