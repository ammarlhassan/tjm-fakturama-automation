from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, File, Form, Header, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .models import OrderInput
from .service import Application, public_job
from .store import ConflictError


class ReviewRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)
    source: OrderInput | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    application = Application(settings)

    @asynccontextmanager
    async def lifespan(_):
        yield
        application.store.engine.dispose()

    api = FastAPI(
        title="TJM Image-to-Cash",
        version="0.1.0",
        description="Durable image jobs; GUI actions run only in the separate interactive worker.",
        lifespan=lifespan,
    )
    api.state.application = application

    @api.middleware("http")
    async def local_origin(request, call_next):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin requests are disabled"}, status_code=403)
        token = os.getenv("ITC_API_TOKEN")
        if token and request.headers.get("authorization") != f"Bearer {token}":
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return await call_next(request)

    @api.exception_handler(ConflictError)
    async def conflict(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @api.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse({"detail": "Job not found"}, status_code=404)

    @api.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @api.get("/health")
    def health():
        from .doctor import diagnose
        from .worker_runtime import observe_workers

        workers = observe_workers(application.settings)
        desktop = diagnose(application.settings, brief=True)
        ownership = application.store.worker_health()
        return {
            "service": "ready",
            "desktop": desktop,
            "workers": workers,
            "desktop_worker_ready": any(
                worker["fresh"] and worker["role"] in {"desktop", "all"} for worker in workers
            )
            and desktop["interactive_desktop"]
            and len(desktop["fakturama_windows"]) == 1
            and not ownership["workspaces"],
            **ownership,
        }

    @api.post("/jobs", status_code=202)
    async def submit(
        image: Annotated[UploadFile, File()],
        workspace: Annotated[str, Form()],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        data = await image.read(application.settings.max_upload_bytes + 1)
        job_id, duplicate = await run_in_threadpool(
            application.submit, data, workspace, key=idempotency_key
        )
        return {"job_id": job_id, "duplicate": duplicate, "status_url": f"/jobs/{job_id}"}

    @api.get("/jobs/{job_id}")
    def status(job_id: str):
        return public_job(application.store.get(job_id))

    @api.post("/jobs/{job_id}/review")
    def review(job_id: str, body: ReviewRequest):
        return public_job(application.review(job_id, body.source, body.reason))

    @api.post("/jobs/{job_id}/resume", status_code=202)
    def resume(job_id: str):
        return public_job(application.queue_resume(job_id))

    @api.get("/jobs/{job_id}/report", response_class=HTMLResponse)
    def report(job_id: str):
        return application.report(job_id).read_text(encoding="utf-8")

    return api
