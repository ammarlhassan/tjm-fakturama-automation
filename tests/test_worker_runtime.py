from datetime import timedelta

from image_to_cash.artifacts import write_json
from image_to_cash.models import utcnow
from image_to_cash.worker_runtime import advertise, observe_workers


def test_worker_liveness_stops_cleanly_and_rejects_stale_heartbeat(settings):
    with advertise(settings, "desktop"):
        workers = observe_workers(settings)
        assert len(workers) == 1 and workers[0]["fresh"]
    assert not observe_workers(settings)[0]["fresh"]
    write_json(
        settings.runtime / "workers/stale.json",
        {
            "pid": 0,
            "role": "desktop",
            "state": "running",
            "last_seen": (utcnow() - timedelta(seconds=30)).isoformat(),
        },
    )
    assert not any(worker["fresh"] for worker in observe_workers(settings))
