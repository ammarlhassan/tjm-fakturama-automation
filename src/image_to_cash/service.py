from __future__ import annotations

from pathlib import Path

from filelock import FileLock

from .adapters import DesktopAdapter, SimulatedAdapter
from .artifacts import ingest, write_json
from .config import Settings
from .extraction import ExtractionPipeline, Provider
from .models import CompletionReceipt, OrderInput, ReviewRequired, Stage
from .store import ConflictError, Store
from .validation import validate_order
from .workflow import ABANDONED_DRAFT, Workflow


class Application:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.settings.prepare()
        self.store = Store(self.settings.db_url)

    def submit(
        self,
        data: bytes,
        workspace: str,
        *,
        key: str | None = None,
        mode: str = "live",
        execute: bool = True,
    ) -> tuple[str, bool]:
        self.settings.workspace_path(workspace)
        digest, path = ingest(data, self.settings)
        return self.store.submit(workspace, digest, str(path), key=key, mode=mode, execute=execute)

    def extract(self, job_id: str, *, provider: Provider | None = None) -> dict:
        with FileLock(str(self.settings.runtime / "locks" / f"job-{job_id}.lock"), timeout=0):
            job = self.store.get(job_id)
            if job["source"] and job["stage"] not in {Stage.RECEIVED, Stage.EXTRACTED}:
                return job
            output = self.settings.runtime / "jobs" / job_id
            try:
                result = ExtractionPipeline(self.settings, provider).run(
                    Path(job["image_path"]), output
                )
                source = result.extraction.order
                validation = validate_order(source)
                self.store.update(
                    job_id,
                    source=source.model_dump(mode="json"),
                    extraction=result.model_dump(mode="json"),
                    validation=validation.model_dump(mode="json"),
                )
                self.store.transition(job_id, Stage.EXTRACTED)
                findings = result.disagreements + [
                    f.model_dump(mode="json") for f in validation.findings
                ]
                if findings:
                    self.store.update(
                        job_id,
                        stage=Stage.NEEDS_REVIEW,
                        review={
                            "code": "EXTRACTION_REVIEW",
                            "message": "Independent readings or deterministic validation did not reconcile",
                            "findings": findings,
                            "resolved": False,
                        },
                    )
                else:
                    other = self.store.check_reference(
                        job_id, source.external_reference or "", source.model_dump(mode="json")
                    )
                    if other:
                        self._complete_duplicate(job_id, other)
                    else:
                        # A resolved note such as a provider retry is now history; the
                        # journal keeps it, and a job's current review must not show it.
                        self.store.update(job_id, review=None)
                        self.store.transition(job_id, Stage.VALIDATED)
            except (ReviewRequired, ConflictError) as exc:
                self.store.update(
                    job_id,
                    stage=Stage.NEEDS_REVIEW,
                    review={
                        "code": getattr(exc, "code", "REFERENCE_CONFLICT"),
                        "message": str(exc),
                        "resolved": False,
                        **getattr(exc, "evidence", {}),
                    },
                )
            except Exception as exc:
                self.store.update(
                    job_id,
                    stage=Stage.NEEDS_REVIEW,
                    review={
                        "code": "EXTRACTION_FAILURE",
                        "message": f"Extraction failed ({type(exc).__name__}); inspect local configuration and retry through review/resume",
                        "resolved": False,
                    },
                )
            self.report(job_id)
            return self.store.get(job_id)

    def _complete_duplicate(self, job_id: str, original_job_id: str) -> None:
        existing = self.store.get(original_job_id)
        receipt = CompletionReceipt.model_validate(existing["receipt"])
        self.store.update(
            job_id,
            receipt=receipt.model_copy(update={"job_id": job_id}).model_dump(mode="json"),
            review=None,
            execute_requested=False,
        )
        self.store.transition(job_id, Stage.SUCCEEDED)
        self.store.log(
            job_id,
            Stage.SUCCEEDED,
            "duplicate_reference",
            "observed",
            {"original_job_id": original_job_id},
        )

    def review(self, job_id: str, source: OrderInput | None, reason: str) -> dict:
        if not reason.strip():
            raise ValueError("A review resolution must explain the evidence or correction")
        with FileLock(str(self.settings.runtime / "locks" / f"job-{job_id}.lock"), timeout=0):
            job = self.store.get(job_id)
            if job["stage"] != Stage.NEEDS_REVIEW:
                raise ConflictError("Only a job awaiting review can be corrected")
            corrected = source or (
                OrderInput.model_validate(job["source"]) if job["source"] else None
            )
            if corrected is None:
                retryable = {
                    "API_KEY_MISSING",
                    "EXTRACTION_FAILURE",
                    "VISION_NETWORK_FAILURE",
                    "VISION_HTTP_ERROR",
                    "VISION_PROVIDER_ERROR",
                    "VISION_INCOMPLETE",
                }
                if (job["review"] or {}).get("code") not in retryable:
                    raise ValueError(
                        "No extracted source exists; provide the reviewed complete order"
                    )
                revision = job["revision"] + 1
                self.store.log(
                    job_id,
                    Stage.NEEDS_REVIEW,
                    "technical_review",
                    "observed",
                    {
                        "revision": revision,
                        "reason": reason,
                        "prior_review": job["review"],
                        "source_changed": False,
                    },
                )
                self.store.update(
                    job_id,
                    revision=revision,
                    review={**(job["review"] or {}), "resolved": True, "resolution": reason},
                )
                self.report(job_id)
                return self.store.get(job_id)
            validation = validate_order(corrected)
            if not validation.accepted:
                raise ValueError(
                    "Corrected source still fails deterministic validation: "
                    + "; ".join(f.message for f in validation.findings)
                )
            other = self.store.check_reference(
                job_id, corrected.external_reference or "", corrected.model_dump(mode="json")
            )
            revision = job["revision"] + 1
            self.store.log(
                job_id,
                Stage.NEEDS_REVIEW,
                "human_review",
                "observed",
                {
                    "revision": revision,
                    "reason": reason,
                    "before": job["source"],
                    "after": corrected.model_dump(mode="json"),
                    "prior_review": job["review"],
                },
            )
            self.store.update(
                job_id,
                source=corrected.model_dump(mode="json"),
                validation=validation.model_dump(mode="json"),
                revision=revision,
                review={**(job["review"] or {}), "resolved": True, "resolution": reason},
            )
            if other:
                self._complete_duplicate(job_id, other)
            self.report(job_id)
            return self.store.get(job_id)

    def queue_resume(self, job_id: str) -> dict:
        job = self.store.get(job_id)
        if job["mode"] == "desktop-probe":
            raise ConflictError(
                "Measurement-only adapter probes cannot be resumed as document workflows"
            )
        if (job["review"] or {}).get("code") == ABANDONED_DRAFT:
            raise ConflictError("This job's draft was abandoned; submit a new source instead")
        if job["stage"] == Stage.SUCCEEDED:
            return job
        if job["stage"] == Stage.NEEDS_REVIEW and not (job["review"] or {}).get("resolved"):
            raise ConflictError("Record an explicit review resolution before resume")
        if not job["source"]:
            self.store.update(job_id, stage=Stage.RECEIVED, execute_requested=True)
        else:
            self.store.update(job_id, stage=Stage.RECONCILING, execute_requested=True)
        return self.store.get(job_id)

    def _adapter(self, job: dict, *, crash_at: str | None = None) -> DesktopAdapter:
        if job["mode"] == "simulation":
            return SimulatedAdapter(
                self.settings.workspace_path(job["workspace"]) / "simulation.json",
                crash_at=crash_at,
            )
        from .windows import WindowsAdapter

        return WindowsAdapter(
            self.settings,
            job["workspace"],
            OrderInput.model_validate(job["source"]) if job["source"] else None,
        )

    def abandon(self, job_id: str, reason: str) -> dict:
        """Discard a quarantined job's identified unsaved draft and release its ownership."""
        if not reason.strip():
            raise ValueError("Abandoning a draft must explain the evidence")
        job = self.store.get(job_id)
        adapter = self._adapter(job)
        try:
            result = Workflow(self.settings, self.store, adapter).abandon_draft(job_id, reason)
            self.report(job_id)
            return result
        finally:
            if hasattr(adapter, "close"):
                adapter.close()

    def execute(self, job_id: str, *, resume: bool = False, crash_at: str | None = None):
        job = self.store.get(job_id)
        if job["mode"] not in {"simulation", "live", "reviewed-live"}:
            raise ConflictError("This evidence job is not a document workflow")
        if job["stage"] == Stage.SUCCEEDED:
            return CompletionReceipt.model_validate(job["receipt"])
        try:
            adapter = self._adapter(job, crash_at=crash_at)
        except ReviewRequired as exc:
            self.store.update(
                job_id,
                stage=Stage.NEEDS_REVIEW,
                review={"code": exc.code, "message": str(exc), "resolved": False},
            )
            self.report(job_id)
            return None
        try:
            receipt = Workflow(self.settings, self.store, adapter).run(job_id, resume=resume)
            self.report(job_id)
            return receipt
        finally:
            if hasattr(adapter, "close"):
                adapter.close()

    def report(self, job_id: str) -> Path:
        from .reporting import build_report

        job = self.store.get(job_id)
        folder = self.settings.runtime / "jobs" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        public = public_job(job)
        write_json(folder / "job.json", public)
        return build_report(job, self.store.events(job_id), folder)


def public_job(job: dict) -> dict:
    return {
        key: job.get(key)
        for key in (
            "id",
            "workspace",
            "mode",
            "stage",
            "last_verified_stage",
            "external_reference",
            "revision",
            "review",
            "validation",
            "receipt",
            "created_at",
            "updated_at",
        )
    }
