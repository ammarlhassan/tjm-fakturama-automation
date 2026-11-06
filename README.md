# TJM Image-to-Cash

Turns one order image into a saved, verified Fakturama Order and linked Invoice through Fakturama's own UI, with a durable journal and a portable evidence report.

## Result on the supplied image

The unchanged official image completed end to end in a brand-new Fakturama workspace that started with no master data: **Order PO000001, Invoice INV000001, Debtor CUST000001**. It needed no operator review. Gemini returned HTTP 503 for about 14 minutes first, so the run retried five times within its limit before its one successful reading, and desktop work began only after that. Values below were read back from the saved records.

| Check | Result |
| --- | --- |
| Reference | `WEB-2026-0714-A17` |
| Totals | EUR 570.00 net, 108.30 VAT, 678.30 gross |
| Items | CHR-ERG-01 x 2 at 250.00, 10% discount, 450.00; MAT-DESK-02 x 3 at 40.00, 120.00 |
| Addresses | Billing Friedrichstrasse 88, 10117 Berlin; delivery Northstar Office Warehouse, Beusselstrasse 44, 10553 Berlin |
| Payment | Bank Transfer, literal PAID, 2026-07-18, value 678.30 |
| Documents | Invoice created from the Order's follow-up action; Invoice paid and Order open; both in the unfiltered This transaction group |
| Masters created | Debtor, Bank Transfer method, VAT 19%, two Products |
| Repeat submission | Same job and receipt; no duplicate documents |

**One flagged discrepancy.** The source email `marta.klein@example.test` uses a reserved domain that Fakturama 2.2.0 will not store, so the saved Debtor's email is blank. The source value stays unchanged, and the receipt and report carry a `SOURCE_EMAIL_NOT_PERSISTED` warning and do not claim complete source-field agreement. Only a `.test` email gets this exception. Any other difference stops for review, and `ITC_EMAIL_POLICY=strict` makes this one stop too.

The recorded desktop runs predate the latest recovery and review fixes. Those fixes have simulator and temporary-database regression coverage, with no new Fakturama acceptance run. The [source-change record](artifacts/source-change-after-r1.json) identifies the tested builds and changes.

## What to open

- [Annotated screenshots](artifacts/submission-demo.html) of the official run, also as a [PDF](artifacts/submission-evidence.pdf)
- [Design document](docs/design.pdf), built from [its source](docs/design.md)
- [Evidence index](artifacts/index.html), with every run and report
- [Requirement status](docs/requirements-status.md) against the brief, step by step
- [Verification limits](docs/limitations.md), the authoritative scope boundary
- [Evidence tour and reproduction](docs/acceptance.md)

## Skipped or not established

- **Real desktop benchmark:** 7 of the 27 planned runs, with no repeats and no 30-line run that creates masters. See [Measured results](#measured-results).
- **Display and session:** acceptance covers one profile, 1920x1200 at 125%. Other scales, smaller windows, a locked session and a killed application process are untested.
- **Inputs:** German-labelled layouts and non-zero shipping are unsupported and stop for review. Address specification/district fields are neither extracted nor populated and have no dedicated detection guard; orders requiring them need manual handling. Real scanned or photographed orders have not been evaluated. Generated evaluation truth still needs human review.
- **Providers:** the OpenAI adapter has never made a live call. Gemini availability is an external dependency, and three release scenarios used explicit input because of it.
- **Other:** Office export and multi-host or PostgreSQL operation are unverified. Setup has run only on the development machine, not a clean one.

## Written question

### If you had 3 more hours, what would you do for this task?

I would spend the first hour on the measured extraction weaknesses: false reviews for a discount above the item table and for harmless image changes, and automatic backoff in the worker, because Gemini returned HTTP 503 for long stretches during acceptance. I would measure against a fresh held-out set before changing parser or normalization rules.

The second hour would test the desktop outside its one recorded profile: other display scales and window sizes, a lost session, and duplicate or conflicting masters. Failures there would take priority over the third hour.

The third hour would profile row entry, which took about 33 seconds per row and 80% of a 30-line run, then validate one optimization against the same complete workload without weakening any check.

## Quick start: no API key or Fakturama required

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from the repository root:

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\TJM-ImageToCash\environment"
uv sync --frozen --extra windows --extra rapidocr
.\itc.cmd doctor
.\itc.cmd run fixtures/sample.visible.png --workspace replay-demo --simulation --replay fixtures/sample.reviewed.json
```

RapidOCR downloads its local models on first use. The report path is printed at the end. The report and receipt label this as a simulated desktop and a manually reviewed extraction; it demonstrates orchestration and reporting, not Fakturama.

On Linux or macOS, use `uv run --extra rapidocr itc ...` for extraction, API, tests and simulation. Live desktop execution is Windows only. This Linux and macOS path is not exercised by the recorded evidence.

If Windows application control rejects a SQLAlchemy native extension, as it did here, install the pure-Python build: set `DISABLE_SQLALCHEMY_CEXT=1` and add `--no-binary-package sqlalchemy` to the sync command. No security policy change is needed.

## Running against Fakturama

The tested versions are in [environment.json](artifacts/environment.json): Windows x64, Fakturama 2.2.0, Temurin Java 21.0.13+9, Tesseract 5.5.3, Python 3.12.

The scripts need the environment that `uv sync` created, which is the one `itc.cmd` uses. Set this in your terminal first:

```powershell
$python = "$env:LOCALAPPDATA\TJM-ImageToCash\environment\Scripts\python.exe"
```

1. **Install Fakturama 2.2.0** from [fakturama.info](https://www.fakturama.info/downloads/). `ITC_FAKTURAMA` overrides its discovered path.
2. **Install Tesseract 5 with `eng` and `deu` data.** Live desktop runs require it, because it reads table headers. `ITC_TESSERACT` overrides its path. RapidOCR handles extraction OCR.
3. **Install Java 21.** Run `& $python scripts/prepare_tools.py java`, which downloads Temurin into `%LOCALAPPDATA%\TJM-ImageToCash\tools\java`, verifies the vendor checksum and records its provenance. `launch` uses it automatically, or `ITC_JAVA` can point to a `javaw.exe`. Fakturama's bundled runtime was not the tested one. `prepare_tools.py` can also download the Fakturama and Tesseract installers.
4. **Keep the runtime outside OneDrive.** The default is `%LOCALAPPDATA%\TJM-ImageToCash`.
5. **Open a dedicated workspace** in an unlocked interactive session, with one Fakturama window open:

```powershell
.\itc.cmd launch --workspace acceptance
# First launch only: answer the working-directory dialog.
& $python scripts/init_workspace.py --workspace acceptance
.\itc.cmd doctor --workspace acceptance
```

6. **Configure a vision provider and run the official image:**

```powershell
.\itc.cmd configure gemini
.\itc.cmd run fixtures/sample.visible.png --workspace acceptance
```

The prompt hides the key and stores it in the current Windows user's environment. Never put keys in source files or command arguments. The tested model is `gemini-3.5-flash-lite`; `ITC_PROVIDER`, `ITC_GEMINI_MODEL` and `ITC_MODEL` configure it. A provider failure stops the job for review before any desktop work, and `.\itc.cmd review` followed by `.\itc.cmd resume` retries it.

**Changes this makes in the workspace.** The adapter sets four Fakturama Preferences through the UI, and they persist: Germany currency locale, cash rounding off, two currency decimals, and unique-item immediate takeover off. It uses the clipboard while reading tables and restores text afterwards. It resizes selector dialogs and maximizes the window, and it needs that desktop session to itself while running. It never writes Fakturama's database or files directly.

## CLI and HTTP API

```powershell
.\itc.cmd extract fixtures/sample.visible.png --workspace demo
.\itc.cmd status JOB_ID
.\itc.cmd review JOB_ID --reason "Reason and supporting evidence" --corrections reviewed-order.json
.\itc.cmd resume JOB_ID
.\itc.cmd abandon JOB_ID --reason "Evidence that nothing was saved"
.\itc.cmd report JOB_ID
.\itc.cmd serve
# In another interactive terminal:
.\itc.cmd worker
```

`abandon` discards one positively identified unsaved Order draft, verifies that nothing was saved for its reference, and releases the quarantine. It refuses any job with a Save or follow-up intent; those must be resumed.

The HTTP server binds to `127.0.0.1:8000`. It accepts image uploads, not paths supplied by remote clients. Extraction and desktop work happen in a separate worker, and submission returns immediately.

```powershell
curl.exe -F "workspace=demo" -F "image=@fixtures/sample.visible.png" -H "Idempotency-Key: sample-1" http://127.0.0.1:8000/jobs
curl.exe http://127.0.0.1:8000/jobs/JOB_ID
```

Routes: `POST /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/review`, `POST /jobs/{id}/resume`, `GET /jobs/{id}/report` and `GET /health`. Interactive schemas are at `/docs`. Set `ITC_API_TOKEN` to require a bearer token. Cross-origin browser requests are rejected.

An identical image in the same workspace returns its existing job. Every accepted idempotency key stays bound to its image, so reusing one for another image returns HTTP 409. Repeated external references are compared against normalized source content. A timeout after Save never authorizes another Save or follow-up Invoice: resume first reconciles the application's saved records.

## Measured results

| Measure | Result | Evidence |
| --- | --- | --- |
| Official image, final run | 22 of 22 acceptance checks, 511 desktop seconds, no operator review | [Checks](artifacts/official-sample-live/verification.json) |
| Live extraction, synthetic documents | 12 of 12 development and 12 of 12 held-out exact, no incorrect acceptance, about 9 seconds per document at the median | [Accuracy and usage](artifacts/accuracy-and-usage.json) |
| Image changes and bad inputs | Two of four harmless image changes stopped for review, all three negative cases stopped, no incorrect acceptance | [Robustness](artifacts/robustness/results.json) |
| Real desktop scenarios | Clean 293 s, recovery after Order Save 319 s, recovery with an unsaved Invoice 351 s, unpaid 292 s, new masters 432 s. All verified with no operator review; the clean and unsaved-Invoice runs had live extraction, the other three explicit input | [Evidence index](artifacts/index.html) |
| Real HTTP API with a separate worker | One complete success, PO000033 / INV000033, with duplicate and reused-key handling, after four provider retries | [Result](artifacts/final-acceptance/R1/api/verification.json) |
| Gemini availability | HTTP 503 on 174 of 183 recorded requests on 5 November | [Summary](artifacts/provider-availability.json) |

**Real desktop benchmark:** seconds per verified Order and Invoice.

| Lines | Existing masters | Mixed masters | New masters |
| --- | --- | --- | --- |
| 2 | 306 | 342 | 411 |
| 10 | 560 | 597 | 670 |
| 30 | 1,230 | Not measured | Not measured |

These are [7 of the 27 planned runs](artifacts/desktop-benchmark/R1/results.json), one per case, all verified, on a warm Fakturama with no extraction time. Each document repeats two SKUs, so 30 lines do not create 30 masters. Line values are whole cents, because sub-cent lines can stop on a [one-cent total difference](artifacts/rounding/cumulative-line-finding.json). Each added row costs about 33 seconds.

Generated ground truth still needs human review, and these small synthetic evaluations do not establish accuracy on real invoices. No transformed variant counts as an independent document.

## Validation and evaluation

```powershell
uv run pytest -q
uv run ruff check src scripts tests
uv run ruff format --check src scripts tests
uv run mypy src/image_to_cash
.\itc.cmd evaluate --engine rapidocr --split development
.\itc.cmd evaluate --engine rapidocr --split heldout
.\itc.cmd benchmark
```

The [recorded checks](artifacts/core-checks.json) show 190 tests passing, with Ruff and mypy clean. Evaluation and benchmark commands write under the runtime folder unless given `--output`, so reruns do not overwrite published results. `evaluate --live` also consumes hosted API quota. `benchmark` measures the simulator only; real desktop timing comes from `scripts/benchmark_desktop.py`.

Money uses decimal strings at boundaries and `Decimal` internally. Only literal `PAID` enables payment. Ambiguous or unsupported inputs enter review. A stop after UI work quarantines the workspace and session until it is reconciled; lease expiry never authorizes a competing writer.

## How this was built

Codex and Claude Code assisted with requirements analysis of the rendered PDF pages, implementation, tests, dependency setup and desktop investigation. Gemini is the runtime vision provider. Expected fixtures came from explicit source data and a manual transcription of the supplied sample, not from an extractor's output.

See the [design](docs/design.md), [limitations](docs/limitations.md) and [code walkthrough](docs/walkthrough.md).
