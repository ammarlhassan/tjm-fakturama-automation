"""Create portable, explicitly simulated demonstrations without API credentials."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

from image_to_cash.adapters import InjectedCrash
from image_to_cash.artifacts import write_json
from image_to_cash.config import Settings
from image_to_cash.extraction import ReplayProvider
from image_to_cash.models import OrderInput, Stage
from image_to_cash.service import Application

ROOT = Path(__file__).resolve().parents[1]


def main():
    destination = ROOT / "artifacts/demos"
    destination.mkdir(parents=True, exist_ok=True)
    default = Settings()
    default.prepare()
    summaries = []
    with tempfile.TemporaryDirectory(prefix="itc-demonstration-", dir=default.runtime) as folder:
        service = Application(
            Settings(
                runtime=Path(folder),
                tesseract=str(default.runtime / "tools/tesseract/tesseract.exe"),
            )
        )
        try:
            for name, crash in (("simulated-complete", False), ("simulated-recovery", True)):
                job, _ = service.submit(
                    (ROOT / "fixtures/sample.visible.png").read_bytes(), name, mode="simulation"
                )
                service.extract(
                    job, provider=ReplayProvider(ROOT / "fixtures/sample.reviewed.json")
                )
                if service.store.get(job)["stage"] != Stage.VALIDATED:
                    raise RuntimeError(service.store.get(job)["review"])
                if crash:
                    try:
                        service.execute(job, crash_at="save_order")
                    except InjectedCrash:
                        service.store.log(
                            job,
                            Stage.RECONCILING,
                            "injected_process_interruption",
                            "observed",
                            {"after": "Order save side effect", "mode": "simulation"},
                        )
                    before = json.loads(
                        (service.settings.workspace_path(name) / "simulation.json").read_text()
                    )
                    if len(before["documents"]) != 1:
                        raise RuntimeError("Interruption fixture did not save exactly one Order")
                    service.queue_resume(job)
                    service.execute(job, resume=True)
                else:
                    service.execute(job)
                state = json.loads(
                    (service.settings.workspace_path(name) / "simulation.json").read_text()
                )
                record = service.store.get(job)
                if record["stage"] != Stage.SUCCEEDED or len(state["documents"]) != 2:
                    raise RuntimeError(record["review"])
                service.report(job)
                export = destination / name
                shutil.copytree(service.settings.runtime / "jobs" / job, export, dirs_exist_ok=True)
                write_json(export / "journal.json", service.store.events(job))
                write_json(
                    export / "verification.json",
                    {
                        "mode": "SIMULATION",
                        "documents": len(state["documents"]),
                        "debtors": len(state["debtors"]),
                        "products": len(state["products"]),
                        "recovered_after_save": crash,
                    },
                )
                summaries.append(
                    {
                        "name": name,
                        "stage": record["stage"],
                        "mode": "SIMULATION",
                        "report": f"{name}/report.html",
                    }
                )

            source = OrderInput.model_validate_json((ROOT / "fixtures/sample.json").read_text())
            source.payment.payment_date = None
            source.external_reference = "NEGATIVE-MISSING-PAYMENT-DATE"
            spec = importlib.util.spec_from_file_location(
                "fixture_renderer", ROOT / "scripts/generate_fixtures.py"
            )
            renderer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(renderer)
            negative = service.settings.runtime / "negative.png"
            renderer.render(source, negative)
            recording = service.settings.runtime / "negative.reviewed.json"
            write_json(
                recording,
                renderer.recording(
                    source,
                    "Synthetic negative case with a visibly blank required payment date; NOT an API recording",
                ),
            )
            job, _ = service.submit(negative.read_bytes(), "ambiguous-input", mode="simulation")
            record = service.extract(job, provider=ReplayProvider(recording))
            if record["stage"] != Stage.NEEDS_REVIEW or service.store.worker_health()["workspaces"]:
                raise RuntimeError("Ambiguous source must stop before desktop ownership")
            export = destination / "ambiguous-input"
            shutil.copytree(service.settings.runtime / "jobs" / job, export, dirs_exist_ok=True)
            write_json(export / "journal.json", service.store.events(job))
            summaries.append(
                {
                    "name": "ambiguous-input",
                    "stage": record["stage"],
                    "mode": "SIMULATION / reviewed extraction",
                    "report": "ambiguous-input/report.html",
                }
            )
        finally:
            service.store.engine.dispose()
    write_json(
        destination / "manifest.json",
        {"demonstrations": summaries, "live_desktop": False, "live_vision": False},
    )
    links = "".join(
        f'<li><a href="{entry["report"]}">{entry["name"]}</a> — {entry["stage"]}</li>'
        for entry in summaries
    )
    (destination / "index.html").write_text(
        f'<!doctype html><html lang="en"><meta charset="utf-8"><title>TJM reproducible demonstrations</title><style>body{{max-width:850px;margin:60px auto;font:18px/1.6 system-ui;color:#18334c}}a{{color:#21635a}}</style><h1>Reproducible demonstrations</h1><p>These runs use a simulated desktop and manually reviewed extraction. They demonstrate orchestration, recovery, validation, and evidence reporting. They are not live Fakturama or hosted-model results.</p><ul>{links}</ul></html>',
        encoding="utf-8",
    )
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
