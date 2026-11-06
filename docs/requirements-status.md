# Status against the brief

This table follows the visible 11-page brief, which was read as rendered pages and never through its hidden text layer. "Official run" means the [final official run](../artifacts/official-sample-live/report.html): the unchanged image, fresh Gemini calls, a brand-new workspace and no operator review. "Audit" means the [later read-only audit](../artifacts/official-sample-live/later-audit/result.json) of that run's saved records.

## Deliverables

| Brief item | Status | Where |
| --- | --- | --- |
| Design, 1-4 pages, no code: control discovery, extraction, tradeoffs | Done, 3 pages | [design.pdf](design.pdf), built only from [design.md](design.md) |
| Source code with a clear structure in a Git repo | Done | `src/image_to_cash`, `tests`, `scripts`, five commits |
| Setup: dependencies and how to run against Fakturama | Done | [README](../README.md#running-against-fakturama), [environment.json](../artifacts/environment.json), `uv.lock` |
| Annotated screenshots or a short recording | Done: annotated screenshots, no recording | [Interactive](../artifacts/submission-demo.html), [PDF](../artifacts/submission-evidence.pdf) |
| README, including skipped work | Done | [README](../README.md#skipped-or-not-established) |
| Written question: 3 more hours | Done | [README](../README.md#written-question), same text in the design |

## Core flow

| Brief steps | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| 1.1-1.2 | Extract every field with OCR and an LLM | Done for the supported fields | Official sample matches `fixtures/sample.json`; Address specification/district are not represented in the extraction contract |
| 1.3-1.8 | New Order first; keep No.; set Date, Cust.Ref., Net, With VAT; keep it open | Done | Annotated step 7; journal order: `open_order` precedes every master action |
| 2.1-2.4 | Debtor selected through the Order on five exact fields; conflicts stop | Done | Existing-master runs select `CUST000001`; tests cover duplicate and same-party conflicts |
| 2.5-2.8 | New Debtor: proposed ID, names, Main address as Invoice address, second address for delivery | Sample completed; 2.7 partial | Annotated step 2. Supplied Address specification/district fields are unsupported and require manual handling |
| 2.9 | Alias, 0% discount, Net | Done | Annotated step 3 |
| 2.10 | Payment method: search, reuse or create, mapped code, zeros, standard unchanged | Done | Annotated step 4. Created for real: [Bank Transfer](../artifacts/official-sample-live/report.html), [SEPA](../artifacts/desktop-fresh-masters/report.html), [Credit Card](../artifacts/desktop-fresh-masters-development/report.html) |
| 2.11-2.13 | Save, return to the same Order, reselect | Done | Official run journal; the saved Debtor is reopened before selection |
| 3.1-3.3 | Every row through the Product selector by exact SKU | Done | Official run journal; benchmark covers existing and new SKUs |
| 3.4-3.7 | VAT checked or created before New product | Done | Annotated step 5; journal order: `create_vat` precedes `create_product` |
| 3.8-3.12 | Product: SKU, name, description, undiscounted gross, zero cost and stock | Done | Annotated step 6: 297.50 and 47.60 |
| 3.13-3.17 | Quantity, U.Price, VAT, discount, line price for every row | Done | Annotated step 7 |
| 4.1-4.3 | Addresses, rows, 0% discount, free shipping, totals match | Done | Annotated step 7; non-zero shipping is unsupported and stops |
| 4.4-4.5 | Save; one row in Data > Documents with number, Date, Cust.Ref., open state, Total | Done | `verify_document` compares the saved row; audit lists the rows |
| 4.6-4.7 | Invoice from the follow-up action, not the toolbar | Done | Journal `follow_up_invoice`; saved relationship verified |
| 5.1 | Generated Invoice fields kept; copied values confirmed | Done | Annotated step 8; a test covers changed generated dates |
| 5.2 | Invoice payment method; unavailable stops | Done | Annotated step 8; a missing combo choice raises a review |
| 5.3 | Literal PAID sets date and full value; otherwise left clear | Done | Annotated step 8 for PAID. An [UNPAID run](../artifacts/final-acceptance/R1X/unpaid/report.html) left payment clear and saved an unpaid Invoice |
| 5.4-5.6 | Save; Invoice row correct while the Order stays open | Done | Annotated step 9 |
| 5.7 | No Delivery, Correction or Dunning | Done | The flow has no such action |
| Objective | No hardcoded coordinates or fixed layout | Done with a limit | Targets resolve from the live window; see [design](design.md). Only one display profile has recorded acceptance |

## The official sample's email

The source email did not persist: the saved Debtor's email is blank in the official run. This is flagged, not hidden. The receipt has `source_field_agreement: false` and a `SOURCE_EMAIL_NOT_PERSISTED` warning, and the source value is unchanged. The brief's own Figure 2 also appears to show a blank E-Mail beside a filled Telephone, although that image is low resolution.

The same image also completed in the older `integration` workspace as [PO000011 / INV000011](../artifacts/official-sample/report.html). That job used a manual transcription rather than a live model call, and it needed seven recorded resolutions, none of which changed a source value. Its Debtor kept a `.com` email from an earlier compatibility test, which an earlier run changed through the UI, so its warning records `different_existing_value`. Its first contact screenshot captured the delivery tab, so use its [later audit](../artifacts/official-sample/later-audit/result.json) for that field.

## Expanded plan, beyond the brief

These runs use synthetic inputs with new references and a `.com` email, so they are not the official sample. "Explicit source" means the input was supplied directly with no model call, because Gemini was returning HTTP 503 when that scenario ran.

| Item | Status | Evidence |
| --- | --- | --- |
| Clean run on the release source | Done, live | [PO000028](../artifacts/final-acceptance/R1/clean/report.html) |
| Recovery after a real Order Save returns | Done, explicit source | [PO000029](../artifacts/final-acceptance/R1X/order-save/report.html): resume found the saved Order and did not save another |
| Recovery with an unsaved follow-up Invoice | Done, live | [PO000030](../artifacts/final-acceptance/R1/invoice-draft/report.html): resume kept the draft and its generated dates |
| Unpaid Invoice | Done, explicit source | [PO000031](../artifacts/final-acceptance/R1X/unpaid/report.html) |
| New Debtor and Products, mixed VAT, document discount | Done, explicit source | [PO000032](../artifacts/final-acceptance/R1X/fresh-masters/report.html) |
| Real HTTP API with a separate worker | Done, live | [PO000033](../artifacts/final-acceptance/R1/api/verification.json): submission 202, duplicate returned the same job, reused key 409, report 200, after 4 provider retries |
| Real desktop benchmark | Partly done | [7 of the planned 27 runs](../artifacts/desktop-benchmark/R1/results.json), all verified, one run per case. Not measured: repeat runs, and 30 lines with new or mixed masters |
| Provider failure handling | Done, bounded | [Availability summary](../artifacts/provider-availability.json). Each stop is retried by a person or a script; automatic backoff in the worker is not built |

These runs used source hash `99e0c8a2`, and the official run used `51644607`. Later changes cover provider/cache metadata, report wording, idempotency, CLI resume, platform guards, review deduplication, reference reservations, generated-identifier recovery checks and saved Invoice state validation. [The source-change record](../artifacts/source-change-after-r1.json) and [diff](../artifacts/source-change-after-r1.diff) identify them. The Fakturama driver is unchanged; the latest workflow and validation fixes have simulator and database coverage, with no new desktop run.

[Verification limits](limitations.md) is the authoritative scope boundary.
