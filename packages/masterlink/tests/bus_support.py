"""Shared waiting helper for the bus tests.

It lived in conftest.py and was imported from there directly, which pytest
discourages and which breaks outright in a monorepo: two packages each with a
tests/conftest.py put the same module name on sys.path twice.
"""
import time


def wait_until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False
