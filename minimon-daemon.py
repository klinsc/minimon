#!/usr/bin/env python3
"""minimon-daemon - headless data engine for the minimon GNOME extension.

Reuses minimon.py's sensor plumbing and writes an atomic JSON snapshot to
~/.cache/minimon/status.json once a second.
"""
import json
import os
import signal
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import minimon as mm  # noqa: E402

OUT = os.path.expanduser("~/.cache/minimon/status.json")
STOP = threading.Event()


def main():
    global _lock
    _lock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        _lock.bind("\0minimon-daemon-%d" % os.getuid())
    except OSError:
        print("minimon-daemon is already running", file=sys.stderr)
        return 0
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sensors = mm.Sensors()
    host = socket.gethostname().upper()
    last = time.monotonic()
    while not STOP.is_set():
        now = time.monotonic()
        dt, last = now - last, now
        d = sensors.read_all(dt if dt > 0 else 1.0)
        d.update(ts=time.time(), host=host,
                 widget=os.path.join(HERE, "minimon.py"))
        tmp = OUT + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, OUT)
        STOP.wait(1.0)
    try:
        os.unlink(OUT)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
