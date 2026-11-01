import io
import logging

from sqlalchemy import select, text

from app.errors import ProcessingError
from app.extractors.media import (
    extract_audio,
    extract_image,
    validate_audio,
    validate_image,
)
from app.extractors.web import download_html, extract_html
from app.integrations.knowledge import EnrichmentResult, NoteDocument, load_adapter
from app.models import Attachment, Job, Note, utc_now
from app.services.jobs import queue_enrichment
from app.services.storage import Storage

logger = logging.getLogger(__name__)


def is_current(job, note):
    if note is None:
        return False
    if job.kind == "cleanup":
        return note.deleted_at is not None
    return note.deleted_at is None and note.content_version == job.input_version


def finish_error(factory, job_id, error):
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        job = session.get(Job, job_id)
        if not job or job.status != "processing":
            return
        note = session.get(Note, job.note_id)
        job.status = "failed" if is_current(job, note) else "cancelled"
        job.error = error if job.status == "failed" else None
        job.finished_at = utc_now()
        if job.status == "failed" and job.kind != "cleanup":
            prefix = "ingestion" if job.kind == "extract" else "enrichment"
            setattr(note, prefix + "_status", "failed")
            setattr(note, prefix + "_error", error)
            note.updated_at = utc_now()
        session.commit()


def process_job(factory, settings, job_id, adapter=None):
    storage = Storage(settings.data_dir)
    try:
        with factory() as session:
            job = session.get(Job, job_id)
            if not job or job.status != "processing":
                return
            note = session.get(Note, job.note_id)
            if not is_current(job, note):
                job.status = "cancelled"
                job.finished_at = utc_now()
                session.commit()
                return
            note_id, kind, version = note.id, job.kind, job.input_version
            document = {
                "note_id": note_id,
                "content_version": version,
                "title": note.title,
                "text": note.extracted_text,
            }
            source_id, source_type = note.source.id, note.source.type
            original_url = note.source.original_url
            attachments = note.source.attachments
            key = attachments[0].storage_key if attachments else None
            metadata = dict(note.source.metadata_json)
            may_have_index = (
                note.enrichment_version is not None
                or session.scalar(
                    select(Job.id)
                    .where(Job.note_id == note_id, Job.kind == "enrich", Job.attempts > 0)
                    .limit(1)
                )
                is not None
            )

        if kind == "cleanup":
            if may_have_index:
                (adapter or load_adapter(settings)).delete_note(note_id)
            storage.remove_source(source_id)
            with factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                note = session.get(Note, note_id)
                if note and note.deleted_at:
                    session.delete(note)
                session.commit()
            return

        if kind == "enrich":
            result = (adapter or load_adapter(settings)).upsert_note(NoteDocument(**document))
            result = EnrichmentResult.model_validate(result)
            if result.note_id != note_id or result.content_version != version:
                raise ProcessingError(
                    "invalid_enrichment_result",
                    "Адаптер вернул результат для другой заметки или версии.",
                )
        else:
            mime = None
            if source_type == "url":
                if key:
                    html = storage.path(key).read_bytes()
                else:
                    html, final_url = download_html(original_url, settings)
                    key, size = storage.save(io.BytesIO(html), source_id, settings.html_max_bytes)
                    metadata["final_url"] = final_url
                    with factory() as session:
                        session.execute(text("BEGIN IMMEDIATE"))
                        note = session.get(Note, note_id)
                        if note and not note.deleted_at:
                            note.source.attachments.append(
                                Attachment(
                                    storage_key=key,
                                    size_bytes=size,
                                    original_name="page.html",
                                    mime_type="text/html",
                                )
                            )
                            note.source.metadata_json = metadata
                        session.commit()
                content = extract_html(html)
            elif source_type == "image":
                mime, details = validate_image(storage.path(key), settings)
                metadata.update(details)
                content = extract_image(storage.path(key), settings)
            elif source_type == "audio":
                mime, details = validate_audio(storage.path(key), settings)
                metadata.update(details)
                content, details = extract_audio(storage.path(key), settings)
                metadata.update(details)
            else:
                raise ProcessingError("unsupported_source", "Неизвестный тип источника.")
            content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
            if not content:
                raise ProcessingError("empty_extracted_text", "В материале не удалось найти текст.")
            if len(content) > settings.text_max_chars:
                raise ProcessingError(
                    "text_too_large", "Извлечённый текст превышает допустимый размер."
                )

        with factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            job = session.get(Job, job_id)
            note = session.get(Note, note_id)
            if not job or job.status != "processing":
                return
            if not is_current(job, note):
                job.status = "cancelled"
            else:
                job.status = "done"
                if kind == "enrich":
                    for field in ("summary", "tags", "category", "entities"):
                        setattr(note, field, result.model_dump()[field])
                    note.enrichment_version = version
                    note.enrichment_status = "ready"
                    note.enrichment_error = None
                else:
                    note.extracted_text = content
                    note.content_version += 1
                    note.ingestion_status = "ready"
                    note.ingestion_error = None
                    note.source.metadata_json = metadata
                    if mime:
                        note.source.attachments[0].mime_type = mime
                    queue_enrichment(session, note, settings)
                note.updated_at = utc_now()
            job.finished_at = utc_now()
            session.commit()
    except ProcessingError as exc:
        finish_error(factory, job_id, exc.as_dict())
    except Exception as exc:
        logger.error("Job %s failed (%s)", job_id, type(exc).__name__)
        finish_error(
            factory,
            job_id,
            {
                "code": "processing_failed",
                "message": "Не удалось обработать материал. Проверьте окружение worker и повторите попытку.",
            },
        )
