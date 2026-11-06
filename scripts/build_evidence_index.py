"""Build a portable entry point from actual measurements; no inferred successes."""

from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "official-sample-live"
SERIES, EXPLICIT_SERIES = "R1", "R1X"
SCENARIOS = (
    ("clean", "Clean run, existing masters"),
    ("order-save", "Recovery after Order Save"),
    ("invoice-draft", "Recovery with an unsaved Invoice"),
    ("unpaid", "Unpaid Invoice"),
    ("fresh-masters", "New Debtor and Products, mixed VAT, document discount"),
)
TEMPLATE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TJM Image-to-Cash evidence</title>
<style>body{margin:0;background:#f2f5f9;color:#16283e;font:16px/1.6 system-ui}header{background:#14263c;color:white;padding:44px max(24px,calc((100vw - 1080px)/2));border-bottom:5px solid #36c3ad}h1{margin:8px 0;font-size:36px}main{max-width:1080px;margin:30px auto;padding:0 24px}section{padding:24px;background:white;border:1px solid #dbe4ed;border-radius:12px;margin-bottom:22px}small{color:#8ee2d2;letter-spacing:2px}h2{font-size:22px;margin-top:0}a{color:#15665b}table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;padding:10px;border-bottom:1px solid #dbe4ed}th{background:#edf2f7}.note{border-left:4px solid #d58a20}li{margin:8px 0}.scroll{overflow:auto}footer{padding:30px;text-align:center;color:#53657d}code{font-size:13px;overflow-wrap:anywhere}</style>
<header><small>TJM LABS · IMAGE-TO-CASH</small><h1>Inspect the result. Follow the evidence.</h1><p>Real model responses, saved application observations, and explicitly labelled test doubles.</p></header>
<main><section class="note"><h2>The official sample</h2><p>The unchanged image completed through live extraction in a brand-new workspace: {{ official }}. Its reserved .test email did not persist, so the saved Debtor's email is blank and the receipt carries a warning instead of claiming full source agreement. All other checks stayed strict.</p></section>
<section><h2>Release acceptance</h2><p>The official run used source hash <code>{{ source_hash }}</code>, and the other runs used <code>{{ r1_hash }}</code>. The current source, <code>{{ final_hash }}</code>, includes later <a href="source-change-after-r1.diff">provider/report metadata, storage, service, recovery and validation fixes</a>. The Fakturama driver is unchanged; the latest workflow fixes have simulator and database regression coverage, with no new desktop acceptance run. Four reports were <a href="source-change-after-r1.json">regenerated</a> after the earlier wording fix; their journals, receipts and checks are unchanged. Desktop seconds exclude provider waits. Inputs other than the official image change the email to .com and use new references, so they are not the official sample. Job revisions recorded for the API run are its provider retries; its journal holds no human review.</p>
<div class="scroll"><table><tr><th>Run</th><th>Status</th><th>Documents</th><th>Gross</th><th>Operator reviews</th><th>Provider retries</th><th>Desktop seconds</th><th>Input</th></tr>{% for row in final %}<tr><td><a href="{{ row.path }}">{{ row.name }}</a></td><td>{{ row.status }}</td><td>{{ row.documents }}</td><td>{{ row.gross }}</td><td>{{ row.reviews }}</td><td>{{ row.retries }}</td><td>{{ row.desktop }}</td><td>{{ row.note }}</td></tr>{% endfor %}</table></div></section>
<section><h2>Real desktop benchmark</h2><p>{{ benchmark.note }} <a href="{{ benchmark.path }}">Raw results</a>.</p><div class="scroll"><table><tr><th>Lines</th><th>Masters</th><th>Verified</th><th>Seconds</th></tr>{% for row in benchmark.rows %}<tr><td>{{ row.lines }}</td><td>{{ row.masters }}</td><td>{{ row.verified }}</td><td>{{ row.seconds }}</td></tr>{% endfor %}</table></div></section>
<section><h2>Live OCR + vision evaluation</h2><p>24 distinct synthetic orders, three layouts, split into 12 development and 12 held-out documents. Measured on 4 November with an earlier build; the prompt, schema and acceptance versions are unchanged in later runs.</p>
<div class="scroll"><table><tr><th>Split</th><th>Documents</th><th>Exact</th><th>Accepted</th><th>Incorrect accepts</th><th>p50 / p95 seconds</th></tr>{% for row in rows %}<tr><td><a href="{{ row.path }}">{{ row.split }}</a></td><td>{{ row.documents }}</td><td>{{ row.exact }}</td><td>{{ row.accepted }}</td><td>{{ row.incorrect }}</td><td>{{ row.p50 }} / {{ row.p95 }}</td></tr>{% endfor %}</table></div><p>Provider: Gemini 3.5 Flash-Lite. Free-tier pricing is published by Google; no billing export was collected. Generated ground truth still needs human review.</p></section>
<section><h2>Robustness checks</h2><p>{{ variants }} image transformations of one source and {{ negatives }} negative cases are reported separately and add no documents to the accuracy corpus. {{ incorrect }} incorrect acceptances were observed. Two of the four harmless transformations stopped for review.</p></section>
<section><h2>Current evidence</h2><ul>{% for name,path in current %}<li><a href="{{ path }}">{{ name }}</a></li>{% endfor %}</ul></section>
<section><h2>Earlier builds, kept as a record</h2><p>Each ran before later fixes. Runs from this session carry their own source hash; older runs carry none. Their reports keep the older banner, which counts provider retries as operator review, so use their journals and verification files for that count.</p><ul>{% for name,path in earlier %}<li><a href="{{ path }}">{{ name }}</a></li>{% endfor %}</ul></section>
</main><footer>Portable evidence · No API key needed to inspect these files</footer></html>"""


def read(relative):
    path = ROOT / "artifacts" / relative
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def seconds(value):
    return f"{value:.0f}" if isinstance(value, int | float) else "Not recorded"


def scenario_result(scenario):
    for series, note in ((SERIES, "Live vision"), (EXPLICIT_SERIES, "Explicit, no model call")):
        data = read(f"final-acceptance/{series}/{scenario}/verification.json")
        if data:
            return series, note, data
    return None, None, {}


def final_rows():
    rows = []
    official = read(f"{RELEASE}/verification.json")
    if official:
        rows.append(
            {
                "name": "Unchanged official image, brand-new workspace",
                "path": f"{RELEASE}/report.html",
                "status": official["status"],
                "documents": f"{official['receipt']['order_number']} / {official['receipt']['invoice_number']}",
                "gross": official["receipt"]["totals"]["gross"],
                "reviews": official["operator_reviews"],
                "retries": official["provider_retries"],
                "desktop": seconds(official["desktop_seconds"]),
                "note": "Live vision, no reused reading; email flagged blank",
            }
        )
    for scenario, label in SCENARIOS:
        series, note, data = scenario_result(scenario)
        if data:
            receipt = data["receipt"]
            rows.append(
                {
                    "name": label,
                    "path": f"final-acceptance/{series}/{scenario}/report.html",
                    "status": data["status"],
                    "documents": f"{receipt['order_number']} / {receipt['invoice_number']}",
                    "gross": receipt["totals"]["gross"],
                    "reviews": data.get("operator_reviews"),
                    "retries": data.get("provider_retries"),
                    "desktop": seconds(data.get("desktop_seconds")),
                    "note": note,
                }
            )
    api = read(f"final-acceptance/{SERIES}/api/verification.json")
    if api:
        receipt = api.get("receipt") or {}
        journal = read(f"final-acceptance/{SERIES}/api/journal.json") or []
        rows.append(
            {
                "name": "Real HTTP API and separate worker",
                "path": f"final-acceptance/{SERIES}/api/verification.json",
                "status": api["status"],
                "documents": f"{receipt.get('order_number', 'None')} / {receipt.get('invoice_number', 'None')}",
                "gross": (receipt.get("totals") or {}).get("gross", "None"),
                "reviews": sum(event["action"] == "human_review" for event in journal),
                "retries": api.get("provider_retries"),
                "desktop": seconds(api.get("worker_seconds")),
                "note": "Live vision; worker seconds include provider waits",
            }
        )
    return rows


def benchmark_rows():
    data = read(f"desktop-benchmark/{SERIES}/results.json")
    summary = data.get("summary", {})
    return {
        "path": f"desktop-benchmark/{SERIES}/results.json",
        "note": (
            f"{summary.get('verified', 0)} of {summary.get('attempted', 0)} attempted documents "
            "verified, with one run per case, so this is a subset of the 27-case matrix. Input is "
            "explicit synthetic data with whole-cent lines, and extraction time is excluded."
        ),
        "rows": [
            {
                "lines": row["lines"],
                "masters": row["masters"],
                "verified": "Yes"
                if row["verified"]
                else f"No: {(row['review'] or {}).get('code')}",
                "seconds": seconds(row.get("seconds")),
            }
            for row in data.get("measurements", [])
        ],
    }


def main():
    rows = []
    for split, path in (
        ("Development", "live-development/gemini-flash-lite.json"),
        ("Held-out", "live-heldout/gemini-flash-lite.json"),
    ):
        data = read(path)
        metrics = data.get("pipeline_metrics", {})
        rows.append(
            {
                "split": split,
                "path": path,
                "documents": data.get("independent_documents", "Pending"),
                "exact": metrics.get("exact_documents", "Pending"),
                "accepted": metrics.get("accepted_documents", "Pending"),
                "incorrect": metrics.get("incorrect_acceptances", "Pending"),
                "p50": round(data.get("p50_seconds", 0), 2),
                "p95": round(data.get("p95_seconds", 0), 2),
            }
        )
    cases = read("robustness/results.json").get("cases", [])
    current = [
        ("Annotated screenshots of the official run", "submission-demo.html"),
        ("Download the annotated evidence PDF", "submission-evidence.pdf"),
        ("Official run: report", f"{RELEASE}/report.html"),
        ("Official run: acceptance checks", f"{RELEASE}/verification.json"),
        ("Read-only audit of saved masters and documents", f"{RELEASE}/later-audit/result.json"),
        ("Provider availability during acceptance", "provider-availability.json"),
        ("What changed in the source after the release suite", "source-change-after-r1.json"),
        ("Real arithmetic boundary probes", "rounding/results.json"),
        ("Finding: sub-cent lines accumulate", "rounding/cumulative-line-finding.json"),
        ("Measured accuracy and token usage", "accuracy-and-usage.json"),
        ("Negative cases and transformed images", "robustness/results.json"),
        ("Three reproducible offline demonstrations (simulation)", "demos/index.html"),
        ("Local checks", "core-checks.json"),
        ("Tested environment", "environment.json"),
        ("Design", "../docs/design.pdf"),
        ("Requirement status against the brief", "../docs/requirements-status.md"),
        ("Verification limits", "../docs/limitations.md"),
    ]
    earlier = [
        (
            "Official image, new workspace, both Gemini responses reused from the cache",
            "official-sample-release/report.html",
        ),
        ("Official image, new workspace, before scale fixes", "official-sample-final/report.html"),
        (
            "Official image, rehearsal that found the empty-workspace defect",
            "official-sample-fresh-rehearsal/report.html",
        ),
        (
            "Official image in the existing workspace, flagged .com email",
            "official-sample/report.html",
        ),
        ("Official job at its earlier email stop", "official-sample-review/report.html"),
        ("Clean run before scale fixes", "final-acceptance/F1/clean/report.html"),
        ("HTTP API success before scale fixes", "final-acceptance/F1/api/report.html"),
        (
            "Recovery after Order Save before scale fixes",
            "final-acceptance/F1X/order-save/report.html",
        ),
        (
            "Recovery with an unsaved Invoice before scale fixes",
            "final-acceptance/F1X/invoice-draft/report.html",
        ),
        ("Unpaid Invoice before scale fixes", "final-acceptance/F1X/unpaid/report.html"),
        ("New masters before scale fixes", "final-acceptance/F1X/fresh-masters/report.html"),
        ("Benchmark: nine 2-line runs, then the 10-line stop", "desktop-benchmark/F1/results.json"),
        ("Benchmark: 10 lines, then the 30-line stop", "desktop-benchmark/F2/results.json"),
        ("Benchmark: stopped first attempt", "desktop-benchmark/A/results.json"),
        ("Clean compatibility run PO000004", "desktop-clean/report.html"),
        ("Recovery after Order Save, earlier", "desktop-recovery/report.html"),
        ("Recovery with an unsaved Invoice, earlier", "desktop-invoice-recovery/report.html"),
        ("New masters with four technical reviews", "desktop-fresh-masters/report.html"),
        ("Real HTTP check stopped by provider 503", "api-live/report.html"),
        ("Reviewed compatibility engineering run", "desktop-compatibility-development/report.html"),
        ("Live-vision engineering run", "desktop-live/report.html"),
        ("Live extraction of the official sample", "live-sample/report.html"),
        ("Expanded-layout false review", "desktop-fresh-extraction-review/report.html"),
    ]

    def present(links):
        return [(name, path) for name, path in links if (ROOT / "artifacts" / path).exists()]

    official = read(f"{RELEASE}/verification.json")
    html = (
        Environment(autoescape=select_autoescape(default=True))
        .from_string(TEMPLATE)
        .render(
            official=(
                f"{official['receipt']['order_number']} and "
                f"{official['receipt']['invoice_number']}, "
                f"EUR {official['receipt']['totals']['gross']} gross, "
                f"{official['operator_reviews']} operator reviews"
            )
            if official
            else "not recorded",
            source_hash=(official.get("source_identity") or {}).get("source_sha256", "unknown"),
            r1_hash=read("source-change-after-r1.json").get("r1_suite_source_sha256", "unknown"),
            final_hash=read("source-change-after-r1.json").get("final_source_sha256", "unknown"),
            final=final_rows(),
            benchmark=benchmark_rows(),
            rows=rows,
            current=present(current),
            earlier=present(earlier),
            variants=sum(case["category"] == "transformed_variant" for case in cases),
            negatives=sum(case["category"] == "negative_case" for case in cases),
            incorrect=sum(
                case.get("pipeline", {}).get("incorrect_acceptance", False) for case in cases
            ),
        )
    )
    path = ROOT / "artifacts/index.html"
    path.write_text(html, encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
