"""Reproducible scores. Synthetic, replay, OCR, and desktop results stay separate."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from .artifacts import write_json
from .config import Settings
from .models import OrderInput, Stage
from .ocr import OCR_VERSION, LayoutParser, recognize
from .validation import DECIMAL_FIELDS, calculate, flatten, line_net, normalize, validate_order


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    left = int(position)
    return ordered[left] + (ordered[min(left + 1, len(ordered) - 1)] - ordered[left]) * (
        position - left
    )


def differences(expected: OrderInput, actual: OrderInput) -> list[dict]:
    # Canonicalization normalizes only declared decimal fields. Identifiers and
    # postcodes must keep their spelling: "01067" is not equivalent to "1067".
    wanted, seen = flatten(expected), flatten(actual)
    findings = []
    for path in sorted(wanted.keys() | seen.keys()):
        left, right = wanted.get(path), seen.get(path)
        equal = left == right
        if isinstance(left, str) and isinstance(right, str):
            equal = (
                Decimal(left) == Decimal(right)
                if path.rsplit(".", 1)[-1] in DECIMAL_FIELDS
                else normalize(left) == normalize(right)
            )
        if not equal:
            findings.append({"field": path, "expected": left, "observed": right})
    return findings


def pipeline_metrics(rows: list[dict]) -> dict:
    measured = [row["pipeline"] for row in rows if "pipeline" in row]
    return {
        "documents_with_predictions": len(measured),
        "exact_documents": sum(row["exact"] for row in measured),
        "accepted_documents": sum(row["accepted"] for row in measured),
        "incorrect_acceptances": sum(row["incorrect_acceptance"] for row in measured),
        "review_documents": sum(not row["accepted"] for row in measured),
        "technical_errors": sum("error" in row for row in rows),
        "attempt_count": sum(len(row["attempts"]) for row in measured),
    }


def evaluate_corpus(
    settings: Settings, *, engine="tesseract", split="development", live=False, output: Path
):
    if split not in {"development", "heldout"}:
        raise ValueError("Choose development or heldout")
    root = Path(__file__).resolve().parents[2] / "fixtures/evaluation"
    if not root.exists():
        root = Path("fixtures/evaluation").resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    rows = []
    pipeline = None
    if live:
        from .extraction import ExtractionPipeline

        pipeline = ExtractionPipeline(settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    configuration = {
        "ocr_engine": engine,
        "parser_version": OCR_VERSION,
        "languages": settings.languages,
        "model": settings.model if live else None,
    }
    if pipeline:
        from .extraction import (
            ACCEPTANCE_VERSION,
            PREPROCESS_VERSION,
            PROMPT_VERSION,
            SCHEMA_VERSION,
        )

        configuration.update(
            vision_provider=pipeline.provider.identity,
            model=getattr(pipeline.provider, "model", settings.model),
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            acceptance_version=ACCEPTANCE_VERSION,
            preprocessing_version=PREPROCESS_VERSION,
        )
    config_hash = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
    if split == "heldout":
        frozen = output.parent / "frozen-configuration.json"
        if frozen.exists() and json.loads(frozen.read_text())["sha256"] != config_hash:
            raise ValueError(
                "Held-out configuration changed; create a separately labelled evaluation directory"
            )
        write_json(
            frozen,
            {
                "configuration": configuration,
                "sha256": config_hash,
                "note": "Frozen before held-out evaluation; generated synthetic ground truth is not human-reviewed by default",
            },
        )
    for entry in manifest["documents"]:
        if entry["split"] != split:
            continue
        expected = OrderInput.model_validate_json(
            (root / entry["truth"]).read_text(encoding="utf-8")
        )
        started = time.perf_counter()
        row = {"id": entry["id"], "layout": entry["layout"], "synthetic": True}
        try:
            ocr = recognize(root / entry["image"], settings, engine=engine)
            extraction = LayoutParser(ocr).parse()
            delta = differences(expected, extraction.order)
            row.update(
                differences=delta,
                field_count=len(flatten(expected)),
                correct_fields=max(
                    0,
                    len(flatten(expected))
                    - sum(difference["field"] in flatten(expected) for difference in delta),
                ),
                exact=not delta,
                ocr_issues=extraction.issues,
                engine=ocr.engine,
                version=ocr.version,
            )
            write_json(
                output.parent / f"{engine}-{entry['id']}-reading.json",
                {
                    "ocr": ocr.model_dump(mode="json"),
                    "extraction": extraction.model_dump(mode="json"),
                },
            )
            if live:
                assert pipeline is not None
                run = pipeline.run(root / entry["image"], output.parent / entry["id"])
                live_delta = differences(expected, run.extraction.order)
                accepted = not run.disagreements and validate_order(run.extraction.order).accepted
                row["pipeline"] = {
                    "accepted": accepted,
                    "exact": not live_delta,
                    "incorrect_acceptance": accepted and bool(live_delta),
                    "differences": live_delta,
                    "attempts": run.attempts,
                    "review_findings": run.disagreements,
                }
        except Exception as exc:
            row.update(error=f"{type(exc).__name__}: {exc}", exact=False)
        row["seconds"] = time.perf_counter() - started
        rows.append(row)
        write_json(
            output, {"status": "in_progress", "configuration": configuration, "documents": rows}
        )
    times = [row["seconds"] for row in rows]
    total_fields = sum(row.get("field_count", 0) for row in rows)
    result = {
        "status": "complete",
        "mode": "live_pipeline" if live else "local_ocr_only",
        "latency_scope": "Evaluation wall time per document, including standalone OCR scoring and the live pipeline when enabled; not HTTP or desktop end-to-end latency",
        "split": split,
        "configuration": configuration,
        "configuration_sha256": config_hash,
        "ground_truth_review": manifest.get("human_visual_review"),
        "independent_documents": len(rows),
        "exact_documents": sum(row["exact"] for row in rows),
        "field_accuracy": sum(row.get("correct_fields", 0) for row in rows) / total_fields
        if total_fields
        else None,
        "errors": sum("error" in row for row in rows),
        "p50_seconds": percentile(times, 0.5),
        "p95_seconds": percentile(times, 0.95),
        "pipeline_metrics": pipeline_metrics(rows) if live else None,
        "documents": rows,
    }
    write_json(output, result)
    return {key: value for key, value in result.items() if key != "documents"}


def benchmark_simulation(settings: Settings, output: Path):
    from .adapters import SimulatedAdapter
    from .store import Store
    from .workflow import Workflow

    sample = Path("fixtures/sample.json")
    if not sample.exists():
        raise ValueError("Run benchmark from the repository root")
    source = OrderInput.model_validate_json(sample.read_text(encoding="utf-8"))
    measurements = []
    with tempfile.TemporaryDirectory(prefix="itc-benchmark-", dir=settings.runtime) as directory:
        local = Settings(runtime=Path(directory))
        local.prepare()
        store = Store(local.db_url)
        try:
            for count in (2, 10, 30):
                for masters in ("new", "existing", "mixed"):
                    for run in range(3):
                        workspace = f"bench-{count}-{masters}-{run}"
                        order = source.model_copy(deep=True)
                        order.external_reference = workspace.upper()
                        order.items = [
                            source.items[index % 2].model_copy(
                                update={"sku": f"SKU-{index:03d}"}, deep=True
                            )
                            for index in range(count)
                        ]
                        for item in order.items:
                            item.printed_net = line_net(item)
                        order.totals = calculate(order)
                        adapter = SimulatedAdapter(
                            local.workspace_path(workspace) / "simulation.json"
                        )
                        if masters != "new":
                            adapter.state["payments"] = [
                                {
                                    "id": order.payment.method,
                                    "name": order.payment.method,
                                    "code": "Credit transfer",
                                }
                            ]
                            adapter.state["debtors"] = [
                                {
                                    "id": "CUST-PREEXISTING",
                                    "customer": order.customer.model_dump(mode="json"),
                                    "payment_method": order.payment.method,
                                }
                            ]
                            adapter.state["products"] = [
                                {
                                    "id": item.sku,
                                    "sku": item.sku,
                                    "vat_percent": str(item.vat_percent),
                                }
                                for index, item in enumerate(order.items)
                                if masters == "existing" or index % 2 == 0
                            ]
                        job, _ = store.submit(
                            workspace,
                            hashlib.sha256(workspace.encode()).hexdigest(),
                            "synthetic.png",
                            mode="simulation",
                        )
                        store.update(
                            job, source=order.model_dump(mode="json"), stage=Stage.VALIDATED
                        )
                        started = time.perf_counter()
                        receipt = Workflow(local, store, adapter).run(job)
                        measurements.append(
                            {
                                "lines": count,
                                "masters": masters,
                                "run": run + 1,
                                "process_temperature": "warm",
                                "seconds": time.perf_counter() - started,
                                "verified": receipt is not None,
                                "review": store.get(job)["review"],
                            }
                        )
        finally:
            store.engine.dispose()
    times = [row["seconds"] for row in measurements]
    result = {
        "mode": "SIMULATION",
        "desktop_throughput": "NOT MEASURED",
        "cold_process": "NOT MEASURED",
        "samples": len(times),
        "verified": sum(row["verified"] for row in measurements),
        "p50_seconds": percentile(times, 0.5),
        "p95_seconds": percentile(times, 0.95),
        "measurements": measurements,
    }
    write_json(output, result)
    return {key: value for key, value in result.items() if key != "measurements"}
