# Inspect and reproduce the evidence

Open the [evidence index](../artifacts/index.html) locally. Each run report embeds its source and application screenshots, and includes the journal, decimal reconciliation, observed documents and receipt. No API key is needed to inspect it.

## A short tour

1. **The official sample:** the [annotated screenshots](../artifacts/submission-demo.html) walk through the [final run](../artifacts/official-sample-live/report.html) step by step. Its [acceptance checks](../artifacts/official-sample-live/verification.json) are the pass criteria, and the [read-only audit](../artifacts/official-sample-live/later-audit/result.json) reads back the masters it created.
2. **The flagged email:** the report opens with the discrepancy. Its [saved contact screenshot](../artifacts/official-sample-live/saved-contact.png) shows the blank E-Mail beside the filled Telephone, read after the contact was closed and reopened.
3. **Recovery:** the index's release table links two controlled stops. One stops orchestration immediately after the real Order Save returns, before its observation is journaled, and resume finds the saved Order instead of saving another. The other stops with an unsaved follow-up Invoice after payment entry, and resume keeps that draft and its generated dates. These stop the orchestrator, not the application process.
4. **A safe stop:** [one-cent total difference](../artifacts/rounding/cumulative-line-finding.json) shows a 30-line draft stopping before Save because Fakturama's total differed from the source by a cent. The [earlier benchmark results](../artifacts/desktop-benchmark/F2/results.json) record that stop.

## Reproducing runs

Each desktop script needs an unlocked session and one Fakturama window on the named workspace, and only one can run at a time. Repeating an image in the same runtime reuses its cached readings unless that cache folder is moved aside; the official script's `verification.json` records `fresh_provider_calls` and `reused_provider_responses`.

These commands need the environment that `uv sync` created, so set this in your terminal first:

```powershell
$python = "$env:LOCALAPPDATA\TJM-ImageToCash\environment\Scripts\python.exe"
```

| Command | What it does |
| --- | --- |
| `& $python scripts/run_official_fresh.py --workspace NAME --output artifacts/NAME` | The official image, live. Its checks require the masters to be created, so use a brand-new workspace. |
| `& $python scripts/complete_official_sample.py` | Completes the older reviewed job in `integration`. |
| `& $python scripts/desktop_acceptance.py --workspace NAME --series TAG` | Clean, both recoveries, unpaid and new masters, with new references under `artifacts/final-acceptance/TAG`. Add `--explicit-source` to run without a model call, labelled as such. |
| `& $python scripts/api_acceptance.py --workspace NAME --series TAG` | Real localhost HTTP, a separate worker and live extraction. |
| `& $python scripts/benchmark_desktop.py --workspace NAME --series TAG` | Real desktop timing. An unexpected stop ends the run and preserves the state. |
| `& $python scripts/audit_saved_records.py --workspace NAME --source SOURCE.json --output DIR --audits TEXT` | Read-only audit of saved masters and documents. It never saves. |
| `& $python scripts/probe_rounding.py --workspace NAME` | Arithmetic in owned drafts, which are discarded with nothing saved. |
| `& $python scripts/demonstrate.py` | Offline demonstrations that call neither a hosted model nor Fakturama. |

Rebuild the published documents with `& $python scripts/build_design.py`, `build_submission_evidence.py`, `provider_availability.py`, `build_evidence_index.py` and `build_checks.py`. `check_docs.py` verifies local links, the shared three-hours answer and key facts. `package_audit.py` builds the package, loads it from the wheel and scans the submission for keys, without printing any.

## Measured configuration

Windows x64, Fakturama 2.2.0, Temurin Java 21.0.13+9, English labels with `en_DE`, Germany currency locale, EUR with two decimals, cash rounding off, and a 1920x1200 display at 125%. Exact versions are in [environment.json](../artifacts/environment.json).

The vision and OCR configuration was frozen before the held-out evaluation; its SHA-256 is `9a3147643ec69f5787db255a8e739bb17045f7b580f3fc4f21b68eaa64df3ce7`. There are 24 distinct generated documents, split 12/12 across three layouts. Human review of their generated ground truth is pending.

## Reading the benchmark

Benchmark inputs are explicitly specified synthetic data, so extraction time and API cost are excluded, and Fakturama is warm. Each document repeats two SKUs across its rows, with differing quantities, prices and discounts. New and mixed profiles create two and one new Products, and the new profile also creates a Debtor, so 30 lines do not mean 30 new Products. Only verified saved records count toward throughput. Never substitute simulator timing or extrapolate from a single run.

Earlier engineering runs are kept with their limitations. Where a receipt has no source hash, it predates this practice and later adapter changes. Current unsupported or unmeasured behavior is in [limitations](limitations.md).
