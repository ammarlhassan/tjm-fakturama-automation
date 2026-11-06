"""Exercise real localhost HTTP submission and a separate live desktop worker.

Uses an isolated job database, the configured API key, and the existing test
Fakturama workspace. Run only when other desktop acceptance work is finished.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
from acceptance_common import PROVIDER_CODES, RETRY_REASON
from generate_fixtures import render

from image_to_cash.artifacts import source_identity, write_json
from image_to_cash.config import Settings
from image_to_cash.evaluation import differences
from image_to_cash.models import OrderInput
from image_to_cash.service import Application, public_job

ROOT = Path(__file__).resolve().parents[1]


def stop_owned_process(process):
    """Windows venv launchers have an interpreter child; stop the owned tree."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        process.terminate()
    process.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="integration")
    parser.add_argument("--port", type=int, default=8017)
    parser.add_argument(
        "--series",
        required=True,
        help="New reference and output under artifacts/final-acceptance/SERIES/api",
    )
    parser.add_argument("--provider-attempts", type=int, default=3)
    parser.add_argument("--provider-wait-seconds", type=int, default=90)
    options = parser.parse_args()
    if not options.series.isalnum() or len(options.series) > 8:
        parser.error("series must contain 1-8 letters or digits")
    settings = Settings()
    settings.prepare()
    output = ROOT / "artifacts/final-acceptance" / options.series / "api"
    output.mkdir(parents=True, exist_ok=True)
    source = OrderInput.model_validate_json(
        (ROOT / "fixtures/compatibility.json").read_text(encoding="utf-8")
    )
    source.external_reference = f"API-WIN-2026-A17-{options.series}"
    image = output / "input.png"
    render(source, image)
    write_json(output / "expected.json", source.model_dump(mode="json"))
    database = settings.runtime / "api-acceptance.sqlite"
    environment = dict(
        os.environ, PYTHONUTF8="1", ITC_DATABASE_URL=f"sqlite:///{database.as_posix()}"
    )
    local = Settings(database_url=environment["ITC_DATABASE_URL"])
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    base = f"http://127.0.0.1:{options.port}"
    # Refuse to reuse an unrelated listener; the subprocess below owns this port.
    import socket

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", options.port))
    headers = {}
    if environment.get("ITC_API_TOKEN"):
        headers["Authorization"] = f"Bearer {environment['ITC_API_TOKEN']}"
    with (output / "server.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "image_to_cash.cli", "serve", "--port", str(options.port)],
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        try:
            with httpx.Client(base_url=base, headers=headers, timeout=15) as client:
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError("API server exited; inspect its local log")
                    try:
                        before = client.get("/health")
                        before.raise_for_status()
                        break
                    except httpx.ConnectError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("API did not become ready")
                upload = dict(
                    data={"workspace": options.workspace},
                    files={"image": (image.name, image.read_bytes(), "image/png")},
                )
                request_headers = {"Idempotency-Key": f"live-api-acceptance-{options.series}"}
                submitted = client.post("/jobs", **upload, headers=request_headers)
                submitted.raise_for_status()
                job_id = submitted.json()["job_id"]
                repeated = client.post("/jobs", **upload, headers=request_headers)
                repeated.raise_for_status()
                assert repeated.json()["duplicate"] and repeated.json()["job_id"] == job_id
                conflict = client.post(
                    "/jobs",
                    data={"workspace": options.workspace},
                    files={
                        "image": (
                            "different.png",
                            (ROOT / "fixtures/sample.visible.png").read_bytes(),
                            "image/png",
                        )
                    },
                    headers=request_headers,
                )
                assert conflict.status_code == 409
                print(
                    json.dumps(
                        {
                            "job_id": job_id,
                            "submission": submitted.status_code,
                            "duplicate_verified": True,
                            "key_conflict": 409,
                        }
                    ),
                    flush=True,
                )
                started = time.perf_counter()
                health_observations = []
                provider_retries = 0
                for attempt in range(1, options.provider_attempts + 1):
                    with (output / "worker.log").open("a", encoding="utf-8") as worker_log:
                        worker = subprocess.Popen(
                            [sys.executable, "-m", "image_to_cash.cli", "worker", "--once"],
                            cwd=ROOT,
                            env=environment,
                            stdout=worker_log,
                            stderr=subprocess.STDOUT,
                            creationflags=flags,
                        )
                        try:
                            last_state = None
                            while worker.poll() is None:
                                if time.perf_counter() - started > 1800:
                                    raise TimeoutError(
                                        "Owned API acceptance worker exceeded 30 minutes; reconcile its job before retrying"
                                    )
                                health = client.get("/health")
                                health.raise_for_status()
                                stage = client.get(f"/jobs/{job_id}").json()["stage"]
                                fresh = any(item["fresh"] for item in health.json()["workers"])
                                state = (stage, fresh, health.json()["desktop_worker_ready"])
                                if state != last_state:
                                    health_observations.append(
                                        {"stage": stage, "health": health.json()}
                                    )
                                    last_state = state
                                time.sleep(2)
                            if worker.returncode:
                                raise RuntimeError("Worker failed; inspect its local log")
                        finally:
                            stop_owned_process(worker)
                    record = client.get(f"/jobs/{job_id}")
                    record.raise_for_status()
                    result = record.json()
                    review = result.get("review") or {}
                    code = review.get("code")
                    if (
                        result["stage"] != "NEEDS_REVIEW"
                        or code not in PROVIDER_CODES
                        or review.get("resolved")
                        or attempt == options.provider_attempts
                    ):
                        break
                    # The documented HTTP review and resume routes drive this bounded retry.
                    provider_retries += 1
                    print(json.dumps({"provider_retry": attempt, "code": code}), flush=True)
                    time.sleep(options.provider_wait_seconds)
                    client.post(
                        f"/jobs/{job_id}/review", json={"reason": RETRY_REASON}
                    ).raise_for_status()
                    client.post(f"/jobs/{job_id}/resume").raise_for_status()
                if result["stage"] != "SUCCEEDED":
                    write_json(
                        output / "verification.json",
                        {
                            "mode": "real localhost HTTP and separate worker",
                            "series": options.series,
                            "status": result["stage"],
                            "submission_http_status": submitted.status_code,
                            "duplicate_submission_verified": True,
                            "idempotency_conflict_http_status": 409,
                            "provider_retries": provider_retries,
                            "complete_workflow_verified": False,
                            "reason": result.get("review"),
                            "source_identity": source_identity(),
                        },
                    )
                    raise RuntimeError(f"API workflow did not complete: {result.get('review')}")
                report = client.get(f"/jobs/{job_id}/report")
                report.raise_for_status()
                service = Application(local)
                try:
                    job = service.store.get(job_id)
                    assert not differences(source, OrderInput.model_validate(job["source"]))
                    shutil.copytree(local.runtime / "jobs" / job_id, output, dirs_exist_ok=True)
                    write_json(output / "job.json", public_job(job))
                    write_json(output / "journal.json", service.store.events(job_id))
                    write_json(
                        output / "verification.json",
                        {
                            "mode": "real HTTP API, separate worker, live vision and real Fakturama",
                            "official_sample": False,
                            "series": options.series,
                            "status": result["stage"],
                            "complete_workflow_verified": True,
                            "provider_retries": provider_retries,
                            "source_identity": source_identity(),
                            "job_id": job_id,
                            "submission_http_status": submitted.status_code,
                            "duplicate_submission_verified": True,
                            "idempotency_conflict_http_status": 409,
                            "report_http_status": report.status_code,
                            "worker_exit_code": worker.returncode,
                            "worker_seconds": time.perf_counter() - started,
                            "job_revisions": job["revision"],
                            "receipt": job["receipt"],
                            "health_before": before.json(),
                            "health_during": health_observations,
                            "health_after": client.get("/health").json(),
                        },
                    )
                finally:
                    service.store.engine.dispose()
                print(
                    json.dumps({"stage": result["stage"], "report": str(output / "report.html")}),
                    flush=True,
                )
        finally:
            stop_owned_process(server)


if __name__ == "__main__":
    main()
