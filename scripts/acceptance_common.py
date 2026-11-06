"""Shared acceptance helpers: bounded provider retries, counted and journaled as such."""

from __future__ import annotations

import json
import time

PROVIDER_CODES = {
    "VISION_PROVIDER_ERROR",
    "VISION_HTTP_ERROR",
    "VISION_NETWORK_FAILURE",
    "VISION_INCOMPLETE",
}
RETRY_REASON = (
    "Bounded retry after provider unavailability: no source value existed and none was changed."
)


def awaiting_provider_retry(record: dict) -> bool:
    """A resolved review note can outlive its stop, so the job's stage decides."""
    review = record["review"] or {}
    return (
        record["stage"] == "NEEDS_REVIEW"
        and review.get("code") in PROVIDER_CODES
        and not review.get("resolved")
    )


def extract_bounded(service, job_id, *, attempts=3, wait_seconds=90):
    """Retry only provider-side failures; any content finding stops immediately."""
    for attempt in range(1, attempts + 1):
        record = service.extract(job_id)
        if not awaiting_provider_retry(record) or attempt == attempts:
            return record
        code = record["review"]["code"]
        print(json.dumps({"provider_retry": attempt, "code": code}), flush=True)
        time.sleep(wait_seconds)
        service.review(job_id, None, RETRY_REASON)
        service.queue_resume(job_id)
    raise AssertionError("unreachable")


def review_counts(events: list[dict]) -> dict:
    """Separate provider retries from reviews that followed an application or code stop."""
    return {
        "provider_retries": sum(event["action"] == "technical_review" for event in events),
        "operator_reviews": sum(event["action"] == "human_review" for event in events),
    }
