from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from .config import Settings
from .models import OrderInput, Stage

app = typer.Typer(
    help="Visible order image → verified Fakturama Order and linked Invoice.", no_args_is_help=True
)


def emit(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.callback()
def main(
    ctx: typer.Context,
    runtime: Annotated[
        Path | None, typer.Option(help="Runtime data directory, outside synced source folders")
    ] = None,
):
    ctx.obj = Settings(runtime=runtime) if runtime else Settings()


def application(ctx):
    from .service import Application

    return Application(ctx.obj)


@app.command("validate")
def validate_source(order: Path):
    """Validate a typed source JSON file without OCR, API calls or desktop access."""
    from .validation import validate_order

    result = validate_order(OrderInput.model_validate_json(order.read_text(encoding="utf-8")))
    emit(result)
    if not result.accepted:
        raise typer.Exit(1)


@app.command()
def doctor(ctx: typer.Context, workspace: str | None = None):
    """Inspect dependencies and desktop readiness; never prints credentials."""
    from .doctor import diagnose

    emit(diagnose(ctx.obj, workspace=workspace))


@app.command()
def launch(ctx: typer.Context, workspace: str = "demo"):
    """Launch the dedicated Fakturama workspace; never closes another workspace."""
    import subprocess

    from .doctor import fakturama_path, java_candidates, observed_windows, workspace_matches_title

    if sys.platform != "win32":
        raise typer.BadParameter("Live Fakturama requires Windows")
    settings = ctx.obj
    folder = settings.workspace_path(workspace)
    (folder / "data").mkdir(parents=True, exist_ok=True)
    windows = observed_windows()
    if windows:
        if len(windows) != 1 or not workspace_matches_title(windows[0]["title"], folder / "data"):
            raise typer.BadParameter(
                "A different Fakturama workspace is open; reconcile and close it first"
            )
        emit({"already_running": True, "workspace": workspace, "pid": windows[0]["pid"]})
        return
    executable = fakturama_path(settings)
    if executable is None:
        raise typer.BadParameter("Install Fakturama 2.2.0 or configure ITC_FAKTURAMA")
    command = [str(executable)]
    candidates = java_candidates(settings)
    if candidates:
        if len(candidates) != 1 or not candidates[0].is_file():
            raise typer.BadParameter("Configure ITC_JAVA to one tested Java 21 javaw.exe")
        command += ["-vm", str(candidates[0])]
    command += ["-nl", "en_DE", "-data", str(folder)]
    process = subprocess.Popen(
        command,
        cwd=executable.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    emit(
        {
            "launched_pid": process.pid,
            "workspace": workspace,
            "working_directory": str(folder / "data"),
            "next": "On first launch choose this working directory. Run doctor after initialization.",
        }
    )


@app.command()
def configure(
    provider: Annotated[str, typer.Argument(help="gemini or openai")] = "gemini",
    provider_option: Annotated[str | None, typer.Option("--provider", hidden=True)] = None,
):
    """Store a provider key locally using a hidden prompt (never in shell history)."""
    provider = provider_option or provider
    if provider not in {"gemini", "openai"}:
        raise typer.BadParameter("Choose gemini or openai")
    variable = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
    token = typer.prompt(f"{variable} (input hidden)", hide_input=True).strip()
    if not token:
        raise typer.BadParameter("Key must not be empty")
    if sys.platform == "win32":
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            winreg.SetValueEx(key, variable, 0, winreg.REG_SZ, token)
            winreg.SetValueEx(key, "ITC_PROVIDER", 0, winreg.REG_SZ, provider)
        typer.echo(
            f"Saved {variable} and provider choice to the current Windows user environment. The key was not printed."
        )
    else:
        typer.echo(
            f"Export {variable} in your shell or secret manager; this helper stores keys only on Windows."
        )


@app.command()
def extract(ctx: typer.Context, image: Path, workspace: str = "demo", replay: Path | None = None):
    """Extract and validate without requesting desktop execution."""
    from .extraction import ReplayProvider
    from .service import public_job

    service = application(ctx)
    try:
        job, duplicate = service.submit(
            image.read_bytes(), workspace, mode="reviewed-live" if replay else "live", execute=False
        )
        result = service.extract(job, provider=ReplayProvider(replay) if replay else None)
        emit({"duplicate": duplicate, **public_job(result), "report": str(service.report(job))})
    finally:
        service.store.engine.dispose()


@app.command()
def run(
    ctx: typer.Context,
    image: Path,
    workspace: str = "demo",
    simulation: bool = False,
    replay: Path | None = None,
):
    """Run a durable job. --simulation is explicitly a test double, never Fakturama."""
    from .extraction import ReplayProvider
    from .service import public_job

    service = application(ctx)
    try:
        mode = "simulation" if simulation else "reviewed-live" if replay else "live"
        job, duplicate = service.submit(image.read_bytes(), workspace, mode=mode)
        result = service.extract(job, provider=ReplayProvider(replay) if replay else None)
        if result["stage"] in {Stage.VALIDATED, Stage.RECONCILING}:
            service.execute(job, resume=result["stage"] == Stage.RECONCILING)
        emit(
            {
                "duplicate": duplicate,
                **public_job(service.store.get(job)),
                "report": str(service.report(job)),
            }
        )
    finally:
        service.store.engine.dispose()


@app.command()
def status(ctx: typer.Context, job_id: str):
    from .service import public_job

    service = application(ctx)
    try:
        emit(public_job(service.store.get(job_id)))
    finally:
        service.store.engine.dispose()


@app.command()
def review(
    ctx: typer.Context,
    job_id: str,
    reason: Annotated[str, typer.Option()],
    corrections: Path | None = None,
):
    """Record an explicit reviewed correction, preserving the original extraction."""
    service = application(ctx)
    try:
        source = (
            OrderInput.model_validate_json(corrections.read_text(encoding="utf-8"))
            if corrections
            else None
        )
        service.review(job_id, source, reason)
        emit(
            {
                "job_id": job_id,
                "resolution_recorded": True,
                "next": "Explicitly resume to reconcile application state",
            }
        )
    finally:
        service.store.engine.dispose()


@app.command()
def abandon(ctx: typer.Context, job_id: str, reason: Annotated[str, typer.Option()]):
    """Discard one identified unsaved draft, verify nothing was saved, and release quarantine."""
    service = application(ctx)
    try:
        emit(service.abandon(job_id, reason))
    finally:
        service.store.engine.dispose()


@app.command()
def resume(ctx: typer.Context, job_id: str):
    from .service import public_job

    service = application(ctx)
    try:
        job = service.queue_resume(job_id)
        if job["source"]:
            service.execute(job_id, resume=True)
        elif service.extract(job_id)["stage"] == Stage.VALIDATED:
            service.execute(job_id)
        emit(public_job(service.store.get(job_id)))
    finally:
        service.store.engine.dispose()


@app.command()
def report(ctx: typer.Context, job_id: str):
    service = application(ctx)
    try:
        typer.echo(str(service.report(job_id)))
    finally:
        service.store.engine.dispose()


@app.command()
def serve(ctx: typer.Context, port: int = 8000):
    """Start the localhost HTTP API; run the interactive worker separately."""
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(ctx.obj), host="127.0.0.1", port=port)


@app.command()
def worker(ctx: typer.Context, once: bool = False, role: str = "all"):
    """Process queued jobs. Roles: extraction, desktop, all (single-machine default)."""
    from filelock import Timeout

    from .store import ConflictError
    from .worker_runtime import advertise

    if role not in {"extraction", "desktop", "all"}:
        raise typer.BadParameter("Role must be extraction, desktop, or all")
    service = application(ctx)
    try:
        with advertise(ctx.obj, role):
            while True:
                for job_id in service.store.queued():
                    try:
                        job = service.store.get(job_id)
                        if job["stage"] in {Stage.RECEIVED, Stage.EXTRACTED} and role != "desktop":
                            job = service.extract(job_id)
                        if (
                            job["stage"] in {Stage.VALIDATED, Stage.RECONCILING}
                            and role != "extraction"
                        ):
                            service.execute(job_id, resume=job["stage"] == Stage.RECONCILING)
                    except (ConflictError, Timeout):
                        continue
                if once:
                    break
                time.sleep(2)
    finally:
        service.store.engine.dispose()


@app.command("inspect")
def inspect_ui(ctx: typer.Context, output: Path | None = None):
    """Capture the live Fakturama accessibility tree for diagnostics."""
    from .windows import inspect_application

    emit(inspect_application(output or ctx.obj.runtime / "inspection"))


@app.command()
def evaluate(
    ctx: typer.Context,
    engine: str = "tesseract",
    split: str = "development",
    live: bool = False,
    output: Path | None = None,
):
    """Score OCR against labelled fixtures; --live also consumes vision API credits.

    Results go under the runtime folder unless --output is given, so a rerun never
    overwrites a published result.
    """
    from .evaluation import evaluate_corpus

    target = output or ctx.obj.runtime / "evaluations" / f"{engine}-{split}.json"
    emit(evaluate_corpus(ctx.obj, engine=engine, split=split, live=live, output=target))


@app.command()
def benchmark(ctx: typer.Context, output: Path | None = None):
    """Measure the simulated workflow; results are never desktop throughput.

    Results go under the runtime folder unless --output is given.
    """
    from .evaluation import benchmark_simulation

    target = output or ctx.obj.runtime / "evaluations" / "simulation-benchmark.json"
    emit(benchmark_simulation(ctx.obj, target))


if __name__ == "__main__":
    app()
