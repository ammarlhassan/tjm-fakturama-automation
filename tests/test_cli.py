import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from image_to_cash.cli import app
from image_to_cash.extraction import ReplayProvider
from image_to_cash.models import ReviewRequired, Stage
from image_to_cash.service import Application

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def job_stopped_by_the_provider(settings):
    """A simulated job with no source yet, whose retry has been recorded in review."""
    service = Application(settings)
    try:
        job, _ = service.submit(
            (FIXTURES / "sample.visible.png").read_bytes(), "demo", mode="simulation"
        )
        service.store.update(
            job,
            stage=Stage.NEEDS_REVIEW,
            review={"code": "VISION_PROVIDER_ERROR", "message": "HTTP 503", "resolved": False},
        )
        service.review(job, None, "The provider recovered")
        return job
    finally:
        service.store.engine.dispose()


def resume(settings, job):
    result = CliRunner().invoke(app, ["--runtime", str(settings.runtime), "resume", job])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_resume_after_a_provider_failure_finishes_the_workflow(settings, monkeypatch):
    reading = ReplayProvider(FIXTURES / "sample.reviewed.json").extract([])[0]

    class Recovered:
        def __init__(self, *args):
            pass

        def run(self, *args):
            return SimpleNamespace(
                extraction=reading,
                disagreements=[],
                model_dump=lambda mode: {"attempts": []},
            )

    monkeypatch.setattr("image_to_cash.service.ExtractionPipeline", Recovered)
    job = job_stopped_by_the_provider(settings)
    resumed = resume(settings, job)
    assert resumed["stage"] == Stage.SUCCEEDED
    assert resumed["receipt"]["order_number"]
    assert resumed["receipt"]["invoice_number"]


def test_resume_stays_off_the_desktop_when_extraction_stops_again(settings, monkeypatch):
    class StillFailing:
        def __init__(self, *args):
            pass

        def run(self, *args):
            raise ReviewRequired("VISION_PROVIDER_ERROR", "HTTP 503")

    def unavailable(*args, **kwargs):
        raise AssertionError("A stopped extraction must not reach the desktop")

    monkeypatch.setattr("image_to_cash.service.ExtractionPipeline", StillFailing)
    monkeypatch.setattr(Application, "execute", unavailable)
    job = job_stopped_by_the_provider(settings)
    resumed = resume(settings, job)
    assert resumed["stage"] == Stage.NEEDS_REVIEW
    assert not resumed["review"]["resolved"]


def test_benchmark_writes_under_runtime_and_leaves_published_results_alone(tmp_path, monkeypatch):
    written = {}

    def benchmark(settings, output):
        written["output"] = output
        return {"mode": "SIMULATION"}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("image_to_cash.evaluation.benchmark_simulation", benchmark)
    runtime = tmp_path / "runtime"
    result = CliRunner().invoke(app, ["--runtime", str(runtime), "benchmark"])
    assert result.exit_code == 0, result.output
    assert written["output"] == runtime / "evaluations" / "simulation-benchmark.json"
    assert json.loads(result.output)["mode"] == "SIMULATION"
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize(
    "arguments", [["configure", "gemini"], ["configure", "--provider", "gemini"], ["configure"]]
)
def test_configure_accepts_documented_command_without_writing_any_key(monkeypatch, arguments):
    # Stop at empty-key validation before any environment or registry mutation.
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "")
    result = CliRunner().invoke(app, arguments)
    assert "Key must not be empty" in result.output
    assert "unexpected extra argument" not in result.output
