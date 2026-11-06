"""Worker liveness is observational; it never grants a desktop lease."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager

from .artifacts import write_json
from .config import Settings
from .models import utcnow


@contextmanager
def advertise(settings: Settings, role: str):
    settings.prepare()
    path = settings.runtime / "workers" / f"{os.getpid()}-{role}.json"
    stop = threading.Event()

    def record(state):
        write_json(
            path,
            {"pid": os.getpid(), "role": role, "state": state, "last_seen": utcnow().isoformat()},
        )

    def pulse():
        while not stop.wait(2):
            record("running")

    record("running")
    thread = threading.Thread(target=pulse, name="worker-liveness", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=3)
        record("stopped")


def observe_workers(settings: Settings) -> list[dict]:
    from datetime import datetime

    workers = []
    for path in (settings.runtime / "workers").glob("*.json"):
        try:
            worker = json.loads(path.read_text(encoding="utf-8"))
            age = (utcnow() - datetime.fromisoformat(worker["last_seen"])).total_seconds()
            worker["fresh"] = worker["state"] == "running" and 0 <= age <= 10
            workers.append(worker)
        except (OSError, ValueError, KeyError):
            continue
    return workers
