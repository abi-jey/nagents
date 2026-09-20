"""Shared watchdog deadline for progress waits in tests.

A short numeric deadline turns a progress check into a latency assertion: on a
loaded runner the real work can exceed it, so the watchdog cancels unrelated
code and produces an unrelated failure. Reserve short deadlines for tests that
assert a timeout and use this guard for every other wait.
"""

HANG_GUARD = 60.0
