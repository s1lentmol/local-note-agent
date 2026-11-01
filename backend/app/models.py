from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Note(Base):
    __tablename__ = 'notes'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    content_version: Mapped[int] = mapped_column(default=1)
    ingestion_status: Mapped[str] = mapped_column(String(20), default='ready', server_default='ready')
    enrichment_status: Mapped[str] = mapped_column(String(20), default='not_requested', server_default='not_requested')
    enrichment_version: Mapped[int | None]
    summary: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(200))
    tags: Mapped[list] = mapped_column(JSON, default=list, server_default='[]')
    entities: Mapped[list] = mapped_column(JSON, default=list, server_default='[]')
    ingestion_error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enrichment_error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped['Source'] = relationship(cascade='all, delete-orphan', lazy='joined')

    @property
    def source_type(self):
        return self.source.type

    @property
    def attachments(self):
        return self.source.attachments


class Source(Base):
    __tablename__ = 'sources'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    note_id: Mapped[str] = mapped_column(ForeignKey('notes.id', ondelete='CASCADE'), unique=True)
    type: Mapped[str] = mapped_column(String(20), default='text')
    original_text: Mapped[str | None] = mapped_column(Text)
    original_url: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')
    attachments: Mapped[list['Attachment']] = relationship(cascade='all, delete-orphan', lazy='selectin')


class Attachment(Base):
    __tablename__ = 'attachments'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey('sources.id', ondelete='CASCADE'), unique=True)
    original_name: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(300), unique=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def download_url(self):
        return f'/api/v1/attachments/{self.id}/download'


class Job(Base):
    __tablename__ = 'jobs'
    __table_args__ = (
        Index('ix_jobs_queue', 'status', 'created_at'),
        Index('uq_jobs_active', 'note_id', 'kind', 'input_version', unique=True,
              sqlite_where=text("status IN ('queued', 'processing')")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    note_id: Mapped[str] = mapped_column(ForeignKey('notes.id', ondelete='CASCADE'), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    input_version: Mapped[int]
    status: Mapped[str] = mapped_column(String(20), default='queued')
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerState(Base):
    __tablename__ = 'worker_state'

    id: Mapped[int] = mapped_column(primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
