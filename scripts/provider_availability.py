"""Summarize Gemini HTTP outcomes from the local journals, without any credential or content."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from image_to_cash.artifacts import write_json
from image_to_cash.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def main():
    settings = Settings()
    statuses: Counter[int] = Counter()
    stopped_rounds = 0
    jobs_recovered = 0
    jobs_stopped = 0
    first, last = None, None
    for name in ("jobs.sqlite", "api-acceptance.sqlite"):
        path = settings.runtime / name
        if not path.exists():
            continue
        database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for job, review, extraction in database.execute(
            "select id, review, extraction from jobs where created_at >= '2026-11-05 10:00'"
        ):
            attempts = []
            retried = False
            for data, stamp in database.execute(
                "select data, timestamp from journal_events "
                "where job_id=? and action='technical_review' order by id",
                (job,),
            ):
                prior = json.loads(data).get("prior_review") or {}
                attempts.extend(prior.get("transport_attempts") or [])
                stopped_rounds += 1
                retried = True
                first, last = min(first or stamp, stamp), max(last or stamp, stamp)
            for attempt in (json.loads(extraction or "null") or {}).get("attempts", []):
                if not attempt.get("cache_hit"):
                    attempts.extend(attempt.get("transport_attempts") or [])
            current = json.loads(review or "null") or {}
            if current.get("code") == "VISION_PROVIDER_ERROR" and not current.get("resolved"):
                attempts.extend(current.get("transport_attempts") or [])
                jobs_stopped += 1
            elif retried:
                jobs_recovered += 1
            statuses.update(item["http_status"] for item in attempts if "http_status" in item)
    total = sum(statuses.values())
    write_json(
        ROOT / "artifacts/provider-availability.json",
        {
            "provider": "gemini:gemini-3.5-flash-lite",
            "scope": "Jobs created on 2026-11-05 from 12:00 local time, read from the local journals",
            "http_statuses": {str(key): value for key, value in sorted(statuses.items())},
            "requests_recorded": total,
            "share_http_503": round(statuses[503] / total, 3) if total else None,
            "extraction_rounds_stopped_by_the_provider_then_retried": stopped_rounds,
            "jobs_that_recovered_after_a_retry": jobs_recovered,
            "jobs_left_stopped_on_the_provider": jobs_stopped,
            "first_and_last_retry_utc": [first, last],
            "note": "A stopped job makes no desktop change. Counts cover recorded transport attempts only.",
        },
    )
    print(ROOT / "artifacts/provider-availability.json")


if __name__ == "__main__":
    main()
