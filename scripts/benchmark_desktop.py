"""Measure verified desktop workflows; never substitute simulation timing.

The matrix uses two unique SKUs per document, repeated in source order, with
different quantities, transaction prices and discounts. This is intentionally
not a claim about documents containing 30 distinct new Product masters.
Extraction is measured separately; these inputs are explicitly specified
synthetic source data. An unexpected review stops the matrix for reconciliation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

from generate_fixtures import render

from image_to_cash.artifacts import source_identity, write_json
from image_to_cash.evaluation import percentile
from image_to_cash.models import OrderInput, Stage
from image_to_cash.service import Application, public_job
from image_to_cash.validation import calculate, line_net, validate_order

ROOT = Path(__file__).resolve().parents[1]


def summarize(rows):
    measured = [row for row in rows if row.get("seconds") is not None]
    passed = [row for row in measured if row["verified"]]
    seconds = sum(row["seconds"] for row in measured)
    return {
        "attempted": len(rows),
        "verified": len(passed),
        "reviewed": sum(not row["verified"] for row in rows),
        "p50_seconds": percentile([row["seconds"] for row in measured], 0.5) if measured else None,
        "p95_seconds": percentile([row["seconds"] for row in measured], 0.95) if measured else None,
        "verified_orders_per_hour_during_measured_execution": len(passed) * 3600 / seconds
        if seconds
        else None,
        "matrix_wall_seconds": seconds,
    }


def main():
    from decimal import Decimal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="integration")
    parser.add_argument("--lines", nargs="+", type=int, choices=(2, 10, 30), default=[2, 10, 30])
    parser.add_argument(
        "--masters",
        nargs="+",
        choices=("existing", "new", "mixed"),
        default=["existing", "new", "mixed"],
    )
    parser.add_argument("--runs", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--series", default="A")
    options = parser.parse_args()
    if not options.series.isalnum() or len(options.series) > 8:
        parser.error("series must contain 1-8 letters or digits")
    output = ROOT / "artifacts/desktop-benchmark" / options.series
    output.mkdir(parents=True, exist_ok=True)
    service = Application()
    rows = []
    target = output / "results.json"
    if target.exists():
        rows = json.loads(target.read_text(encoding="utf-8"))["measurements"]
    base = OrderInput.model_validate_json(
        (ROOT / "fixtures/compatibility.json").read_text(encoding="utf-8")
    )

    def publish(status):
        write_json(
            target,
            {
                "status": status,
                "mode": "REAL FAKTURAMA DESKTOP",
                "input_provenance": "Explicit synthetic expected data; no extraction/API time included",
                "master_cardinality": "Two unique SKUs per document, repeated across 2/10/30 rows",
                "temperature": "Fakturama warm; new adapter per job, same Python process; first case marks process first-use",
                "cold_fakturama": "Not measured; do not interpret first-use as application cold start",
                "series": options.series,
                "source_identity": source_identity(),
                "requested": {
                    "lines": options.lines,
                    "masters": options.masters,
                    "runs": options.runs,
                },
                "summary": summarize(rows),
                "measurements": rows,
            },
        )

    try:
        for count in options.lines:
            for masters in options.masters:
                for run in range(1, options.runs + 1):
                    case = f"{count}-{masters}-{run}-{options.series}"
                    if any(row["case"] == case and row["verified"] for row in rows):
                        continue
                    source = base.model_copy(deep=True)
                    source.external_reference = "BENCH-" + case.upper()
                    if masters == "new":
                        source.customer.company = "Benchmark Office " + case
                        source.customer.alias = "BENCH-" + case
                        source.customer.email = "benchmark@example.com"
                        source.customer.billing.recipient = source.customer.company
                        source.customer.delivery.recipient = "Benchmark Warehouse " + case
                    source.items = []
                    for index in range(count):
                        item = base.items[index % 2].model_copy(deep=True)
                        if masters == "new" or (masters == "mixed" and index % 2):
                            item.sku = f"B-{case}-{index % 2}".upper()
                        # Whole-cent lines: this measures desktop time, not rounding. See
                        # artifacts/rounding/cumulative-line-finding.json for why that matters.
                        item.quantity = (Decimal("1"), Decimal("0.5"), Decimal("2"))[index % 3]
                        item.unit_net = Decimal("10") + Decimal(index)
                        item.discount_percent = (Decimal("0"), Decimal("20"), Decimal("10"))[
                            index % 3
                        ]
                        item.printed_net = line_net(item)
                        source.items.append(item)
                    source.totals = calculate(source)
                    assert validate_order(source).accepted
                    folder = output / case
                    folder.mkdir(parents=True, exist_ok=True)
                    image = folder / "input.png"
                    render(source, image)
                    write_json(folder / "expected.json", source.model_dump(mode="json"))
                    job_id, duplicate = service.submit(
                        image.read_bytes(), options.workspace, mode="reviewed-live", execute=False
                    )
                    record = service.store.get(job_id)
                    if not duplicate:
                        service.store.update(
                            job_id,
                            source=source.model_dump(mode="json"),
                            external_reference=source.external_reference,
                            stage=Stage.VALIDATED,
                            extraction={
                                "provenance": "explicit synthetic desktop benchmark source",
                                "expected_sha256": hashlib.sha256(
                                    source.model_dump_json().encode()
                                ).hexdigest(),
                            },
                        )
                        service.store.log(
                            job_id,
                            Stage.VALIDATED,
                            "benchmark_expected_source",
                            "observed",
                            {"fixture": str(folder / "expected.json"), "live_extraction": False},
                        )
                    elif record["stage"] != Stage.SUCCEEDED:
                        raise RuntimeError(
                            f"Reconcile previous attempt {job_id} before rerunning this case"
                        )
                    print(
                        json.dumps({"case": case, "job_id": job_id, "phase": "desktop"}), flush=True
                    )
                    started = time.perf_counter()
                    receipt = service.execute(job_id)
                    elapsed = time.perf_counter() - started
                    record = service.store.get(job_id)
                    service.report(job_id)
                    shutil.copytree(
                        service.settings.runtime / "jobs" / job_id, folder, dirs_exist_ok=True
                    )
                    write_json(folder / "job.json", public_job(record))
                    write_json(folder / "journal.json", service.store.events(job_id))
                    repeated, dedup = service.submit(
                        image.read_bytes(), options.workspace, mode="reviewed-live", execute=False
                    )
                    assert repeated == job_id and dedup
                    row = {
                        "case": case,
                        "job_id": job_id,
                        "lines": count,
                        "masters": masters,
                        "run": run,
                        "process_first_use": not rows,
                        "seconds": None if duplicate else elapsed,
                        "verified": receipt is not None,
                        "review": record["review"],
                        "job_revisions": record["revision"],
                        "duplicate_submission_verified": True,
                        "receipt": record["receipt"],
                    }
                    rows.append(row)
                    publish("in_progress")
                    print(
                        json.dumps(
                            {
                                "case": case,
                                "verified": row["verified"],
                                "seconds": row["seconds"],
                                "review": row["review"],
                            }
                        ),
                        flush=True,
                    )
                    if receipt is None:
                        publish("stopped_for_review")
                        raise RuntimeError(
                            "Desktop benchmark stopped; inspect the preserved review and application state"
                        )
        publish("complete_requested_subset")
    finally:
        service.store.engine.dispose()


if __name__ == "__main__":
    main()
