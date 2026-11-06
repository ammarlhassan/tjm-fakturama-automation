from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from platformdirs import user_data_path
from pydantic import BaseModel, Field

EmailPolicy = Literal["flag-reserved-domain", "strict"]


class Settings(BaseModel):
    runtime: Path = Field(
        default_factory=lambda: Path(
            os.getenv("ITC_RUNTIME", user_data_path("TJM-ImageToCash", appauthor=False))
        )
    )
    model: str = Field(default_factory=lambda: os.getenv("ITC_MODEL", "gpt-6-astra"))
    ocr_engine: str = Field(default_factory=lambda: os.getenv("ITC_OCR", "rapidocr"))
    tesseract: str = Field(default_factory=lambda: os.getenv("ITC_TESSERACT", "tesseract"))
    languages: str = "eng+deu"
    request_timeout_seconds: float = 120
    max_transport_retries: int = 2
    desktop_timeout_seconds: float = 30
    database_url: str | None = Field(default_factory=lambda: os.getenv("ITC_DATABASE_URL"))
    max_upload_bytes: int = 20 * 1024 * 1024
    max_image_pixels: int = 20_000_000
    # flag-reserved-domain: finish a reserved .test source email and record what actually
    # persisted. strict: any email difference stops for review. Nothing else is relaxed.
    email_discrepancy_policy: EmailPolicy = Field(
        default_factory=lambda: os.getenv("ITC_EMAIL_POLICY", "flag-reserved-domain"),  # type: ignore[arg-type,return-value]
        validate_default=True,
    )

    def prepare(self) -> None:
        for name in ("jobs", "cache", "workspaces", "locks", "tools", "workers"):
            (self.runtime / name).mkdir(parents=True, exist_ok=True)

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.runtime / 'jobs.sqlite').as_posix()}"

    def workspace_path(self, workspace: str) -> Path:
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", workspace):
            raise ValueError("Workspace must contain 1–64 letters, digits, underscores or hyphens")
        return self.runtime / "workspaces" / workspace
