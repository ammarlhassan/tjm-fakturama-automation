"""Durable journal and atomic job ownership; no transaction spans this DB and the UI."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .models import Stage, WorkflowCheckpoint, utcnow
from .validation import normalized_source


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("workspace", "image_hash"),
        UniqueConstraint("workspace", "idempotency_key"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace: Mapped[str] = mapped_column(String(64), index=True)
    image_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    image_path: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20), default="live")
    stage: Mapped[str] = mapped_column(String(40), default=Stage.RECEIVED)
    last_verified_stage: Mapped[str] = mapped_column(String(40), default=Stage.RECEIVED)
    external_reference: Mapped[str | None] = mapped_column(String(200), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[dict | None] = mapped_column(JSON)
    extraction: Mapped[dict | None] = mapped_column(JSON)
    validation: Mapped[dict | None] = mapped_column(JSON)
    receipt: Mapped[dict | None] = mapped_column(JSON)
    review: Mapped[dict | None] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(100))
    execute_requested: Mapped[bool] = mapped_column(default=False)
    claimed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyKey(Base):
    """Every accepted key stays bound to one job, and through it to one image and mode."""

    __tablename__ = "idempotency_keys"
    workspace: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))


class SourceReference(Base):
    __tablename__ = "source_references"
    workspace: Mapped[str] = mapped_column(String(64), primary_key=True)
    reference: Mapped[str] = mapped_column(String(200), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    content_hash: Mapped[str] = mapped_column(String(64))


class JournalEvent(Base):
    __tablename__ = "journal_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    timestamp: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkspaceOwner(Base):
    __tablename__ = "workspace_owners"
    workspace: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    worker: Mapped[str] = mapped_column(String(100))
    quarantined: Mapped[bool] = mapped_column(default=False)
    heartbeat: Mapped[Any] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConflictError(Exception):
    pass


class Store:
    def __init__(self, url: str):
        self.engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 15}
            if url.startswith("sqlite")
            else {},
            pool_pre_ping=True,
        )
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def sqlite_connect(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)

    def submit(
        self,
        workspace: str,
        image_hash: str,
        image_path: str,
        *,
        key: str | None = None,
        mode: str = "live",
        execute: bool = False,
    ) -> tuple[str, bool]:
        from sqlalchemy.exc import IntegrityError

        with Session(self.engine) as session:
            if key:
                found = self._job_for_key(session, workspace, key)
                if found:
                    if found.image_hash != image_hash or found.mode != mode:
                        raise ConflictError(
                            "Idempotency key was already used with different content or mode"
                        )
                    if execute:
                        found.execute_requested = True
                        session.commit()
                    return found.id, True
            found = session.scalar(
                select(Job).where(Job.workspace == workspace, Job.image_hash == image_hash)
            )
            if found and found.mode != mode:
                raise ConflictError("An image cannot change execution modes in one workspace")
            job_id = found.id if found else str(uuid.uuid4())
            try:
                if found:
                    if execute:
                        found.execute_requested = True
                else:
                    session.add(
                        Job(
                            id=job_id,
                            workspace=workspace,
                            image_hash=image_hash,
                            image_path=image_path,
                            mode=mode,
                            execute_requested=execute,
                        )
                    )
                    session.flush()
                if key:
                    session.add(IdempotencyKey(workspace=workspace, key=key, job_id=job_id))
                session.commit()
            except IntegrityError:
                session.rollback()
                return self.submit(
                    workspace, image_hash, image_path, key=key, mode=mode, execute=execute
                )
            duplicate = found is not None
        if not duplicate:
            self.log(job_id, Stage.RECEIVED, "submit", "observed", {})
        return job_id, duplicate

    @staticmethod
    def _job_for_key(session: Session, workspace: str, key: str) -> Job | None:
        binding = session.get(IdempotencyKey, (workspace, key))
        if binding:
            return session.get(Job, binding.job_id)
        # Journals written before the binding table kept their only key on the job.
        return session.scalar(
            select(Job).where(Job.workspace == workspace, Job.idempotency_key == key)
        )

    def get(self, job_id: str) -> dict:
        with Session(self.engine) as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            return {column.name: getattr(job, column.name) for column in Job.__table__.columns}

    def update(self, job_id: str, **values: Any) -> None:
        values["updated_at"] = utcnow()
        with Session(self.engine) as session:
            result = session.execute(
                update(Job).where(Job.id == job_id).values(**values).returning(Job.id)
            ).scalar_one_or_none()
            if result is None:
                raise KeyError(job_id)
            session.commit()

    def transition(self, job_id: str, stage: Stage, **data: Any) -> None:
        self.update(job_id, stage=stage.value, last_verified_stage=stage.value)
        self.log(job_id, stage, "transition", "observed", data)

    def log(self, job_id: str, stage: Stage | str, action: str, status: str, data: dict) -> int:
        checkpoint = WorkflowCheckpoint(
            stage=Stage(stage), action=action, status=status, observed=data
        )
        with Session(self.engine) as session:
            entry = JournalEvent(
                job_id=job_id,
                stage=checkpoint.stage.value,
                action=checkpoint.action,
                status=checkpoint.status,
                data=checkpoint.model_dump(mode="json")["observed"],
                timestamp=checkpoint.timestamp,
            )
            session.add(entry)
            session.commit()
            return entry.id

    def events(self, job_id: str) -> list[dict]:
        with Session(self.engine) as session:
            entries = session.scalars(
                select(JournalEvent).where(JournalEvent.job_id == job_id).order_by(JournalEvent.id)
            )
            return [
                {
                    "id": e.id,
                    "stage": e.stage,
                    "action": e.action,
                    "status": e.status,
                    "data": e.data,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in entries
            ]

    def claim_job(self, job_id: str, worker: str) -> bool:
        with Session(self.engine) as session:
            result = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.claimed_by.is_(None))
                .values(claimed_by=worker, claimed_at=utcnow())
                .returning(Job.id)
            ).scalar_one_or_none()
            session.commit()
            return result is not None

    def release_job(self, job_id: str, worker: str) -> None:
        with Session(self.engine) as session:
            session.execute(
                update(Job)
                .where(Job.id == job_id, Job.claimed_by == worker)
                .values(claimed_by=None, claimed_at=None)
            )
            session.commit()

    def own_workspace(
        self, workspace: str, job_id: str, worker: str, *, desktop: bool = False
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        with Session(self.engine) as session:
            if desktop:
                other = session.scalar(
                    select(WorkspaceOwner)
                    .join(Job, Job.id == WorkspaceOwner.job_id)
                    .where(WorkspaceOwner.job_id != job_id, Job.mode != "simulation")
                )
                if other:
                    raise ConflictError(
                        f"Desktop session is owned or quarantined by job {other.job_id}; reconcile that job first"
                    )
            owner = session.get(WorkspaceOwner, workspace)
            if owner:
                if owner.job_id != job_id or (owner.worker != worker and not owner.quarantined):
                    raise ConflictError(
                        f"Workspace is reserved by job {owner.job_id}; lease expiry does not transfer ownership"
                    )
                owner.worker, owner.heartbeat = worker, utcnow()
            else:
                session.add(WorkspaceOwner(workspace=workspace, job_id=job_id, worker=worker))
            try:
                session.commit()
            except IntegrityError as exc:
                raise ConflictError("Another worker acquired this workspace") from exc

    def release_workspace(self, workspace: str, job_id: str, *, quarantine: bool) -> None:
        with Session(self.engine) as session:
            owner = session.get(WorkspaceOwner, workspace)
            if owner and owner.job_id == job_id:
                if quarantine:
                    owner.quarantined, owner.heartbeat = True, utcnow()
                else:
                    session.delete(owner)
                session.commit()

    def heartbeat(self, workspace: str, job_id: str) -> None:
        with Session(self.engine) as session:
            session.execute(
                update(WorkspaceOwner)
                .where(WorkspaceOwner.workspace == workspace, WorkspaceOwner.job_id == job_id)
                .values(heartbeat=utcnow())
            )
            session.commit()

    def check_reference(self, job_id: str, reference: str, source: dict) -> str | None:
        import json

        from sqlalchemy.exc import IntegrityError

        fingerprint = hashlib.sha256(
            json.dumps(normalized_source(source), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for attempt in range(2):
            with Session(self.engine) as session:
                job = session.get(Job, job_id)
                if job is None:
                    raise KeyError(job_id)

                def has_document_actions():
                    return (
                        session.scalar(
                            select(JournalEvent.id)
                            .where(
                                JournalEvent.job_id == job_id,
                                JournalEvent.action.in_(
                                    [
                                        "open_order",
                                        "save_order",
                                        "follow_up_invoice",
                                        "save_invoice",
                                    ]
                                ),
                            )
                            .limit(1)
                        )
                        is not None
                    )

                previous_reference = job.external_reference or (job.source or {}).get(
                    "external_reference"
                )
                if previous_reference and previous_reference != reference:
                    if has_document_actions():
                        raise ConflictError(
                            "The external reference cannot change after an Order creation intent. "
                            "Reconcile the original job; an unsaved draft can be abandoned before resubmission."
                        )
                    previous_claim = session.get(
                        SourceReference, (job.workspace, previous_reference)
                    )
                    if previous_claim and previous_claim.job_id == job_id:
                        session.delete(previous_claim)
                claim = session.get(SourceReference, (job.workspace, reference))
                # Upgrade existing journals without discarding their reservations.
                legacy = (
                    session.scalar(
                        select(Job)
                        .where(
                            Job.workspace == job.workspace,
                            Job.external_reference == reference,
                            Job.id != job_id,
                        )
                        .order_by(Job.created_at)
                    )
                    if claim is None
                    else None
                )
                owner = session.get(Job, claim.job_id) if claim else legacy
                alias = None
                if owner and owner.id != job_id:
                    if has_document_actions():
                        raise ConflictError(
                            "A job with document actions cannot reuse another job's receipt; "
                            "reconcile its existing documents first"
                        )
                    if owner.mode != job.mode:
                        raise ConflictError(
                            "Source references cannot share receipts across execution modes"
                        )
                    if owner.content_hash != fingerprint:
                        raise ConflictError(
                            "External reference is already associated with different source content"
                        )
                    if owner.stage != Stage.SUCCEEDED:
                        raise ConflictError("Another unfinished job owns this external reference")
                    alias = owner.id
                if claim is None:
                    session.add(
                        SourceReference(
                            workspace=job.workspace,
                            reference=reference,
                            job_id=alias or job_id,
                            content_hash=fingerprint,
                        )
                    )
                elif claim.job_id == job_id:
                    claim.content_hash = fingerprint
                job.external_reference, job.content_hash, job.updated_at = (
                    reference,
                    fingerprint,
                    utcnow(),
                )
                try:
                    session.commit()
                    return alias
                except IntegrityError:
                    session.rollback()
                    if attempt:
                        raise ConflictError(
                            "Concurrent source-reference reservation could not be reconciled"
                        ) from None
        raise ConflictError("Source-reference reservation failed")

    def queued(self, limit: int = 10) -> list[str]:
        with Session(self.engine) as session:
            return list(
                session.scalars(
                    select(Job.id)
                    .where(
                        Job.execute_requested.is_(True),
                        Job.stage.in_(
                            [Stage.RECEIVED, Stage.EXTRACTED, Stage.VALIDATED, Stage.RECONCILING]
                        ),
                        Job.claimed_by.is_(None),
                    )
                    .order_by(Job.created_at)
                    .limit(limit)
                )
            )

    def worker_health(self) -> dict:
        with Session(self.engine) as session:
            owners = list(session.scalars(select(WorkspaceOwner)))
            now = utcnow().replace(tzinfo=None)
            return {
                "workspaces": [
                    {
                        "workspace": o.workspace,
                        "job_id": o.job_id,
                        "quarantined": o.quarantined,
                        "heartbeat": o.heartbeat.isoformat(),
                        "stale": now - o.heartbeat.replace(tzinfo=None) > timedelta(seconds=60),
                    }
                    for o in owners
                ]
            }
