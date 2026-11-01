from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, joinedload, load_only

from app.errors import ProcessingError, api_error
from app.models import Attachment, Job, Note, Source, new_id, utc_now
from app.schemas import (
    EnrichmentStatus,
    ErrorResponse,
    IngestionStatus,
    JobDetail,
    NoteCreate,
    NoteDetail,
    NoteList,
    NoteSummary,
    NoteUpdate,
    ReindexResponse,
    RetryRequest,
    SourceType,
    Title,
)
from app.services.jobs import cancel_pending, enqueue, queue_enrichment
from app.services.storage import Storage

router = APIRouter(
    prefix="/api/v1/notes",
    tags=["notes"],
    responses={
        422: {"model": ErrorResponse, "description": "Некорректные поля или параметры запроса."},
    },
)
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Заметка не найдена."}}
CONFLICT = {
    409: {
        "model": ErrorResponse,
        "description": "Конфликт версии, состояния или настроек адаптера.",
    }
}


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


Database = Annotated[Session, Depends(get_session)]


def get_note(session, note_id, include_deleted=False):
    note = session.get(Note, str(note_id))
    if note is None or (note.deleted_at and not include_deleted):
        raise api_error(404, "note_not_found", "Заметка не найдена.")
    return note


@router.post(
    "",
    response_model=NoteDetail,
    status_code=201,
    summary="Создать заметку из текста или ссылки",
    description="Текст сохраняется сразу (201). Ссылка ставится в очередь (202). Для URL нужны worker и доступ к сети. "
    "Поддерживаются публичные HTML-страницы без JavaScript и авторизации. Локальные адреса запрещены. "
    "Обработка выполняется асинхронно; опрашивайте GET заметки примерно раз в 2 секунды.",
    responses={202: {"model": NoteDetail, "description": "Импорт ссылки поставлен в очередь."}},
)
def create_note(payload: NoteCreate, request: Request, response: Response, session: Database):
    settings = request.app.state.settings
    session.execute(text("BEGIN IMMEDIATE"))
    is_text = payload.source_type == "text"
    if is_text and len(payload.text) > settings.text_max_chars:
        raise api_error(422, "text_too_large", "Текст превышает настроенный лимит.")
    source = Source(
        type=payload.source_type,
        original_text=payload.text if is_text else None,
        original_url=None if is_text else str(payload.url),
    )
    note = Note(
        title=payload.title,
        extracted_text=payload.text if is_text else None,
        source=source,
        content_version=1 if is_text else 0,
        ingestion_status="ready" if is_text else "queued",
        enrichment_status="not_requested",
    )
    session.add(note)
    session.flush()
    if is_text:
        queue_enrichment(session, note, settings)
    else:
        enqueue(session, note, "extract")
        response.status_code = 202
    session.commit()
    session.refresh(note)
    return NoteDetail.model_validate(note)


@router.post(
    "/import",
    response_model=NoteDetail,
    status_code=202,
    summary="Импортировать изображение или аудио",
    description="Один оригинал на заметку. JPEG/PNG — до 10 MiB и 25 млн пикселей; WAV/MP3 — до 50 MiB и 10 минут "
    "(точные настроенные лимиты в GET /capabilities). Содержимое проверяется worker. "
    "202 подтверждает сохранение файла, а не успешное распознавание. Для OCR нужен Tesseract rus+eng, "
    "для аудио — заранее загруженная локальная модель Whisper. MIME и расширение не считаются доказательством формата.",
    responses={
        413: {"model": ErrorResponse, "description": "Превышен размер загрузки."},
        415: {"model": ErrorResponse, "description": "Пустой или неподдерживаемый файл."},
    },
)
def import_note(
    request: Request,
    session: Database,
    title: Annotated[Title, Form(description="Заголовок заметки.")],
    source_type: Annotated[
        Literal["image", "audio"], Form(description="Тип загружаемого материала.")
    ],
    file: Annotated[UploadFile, File(description="Один JPEG/PNG либо WAV/MP3.")],
):
    settings = request.app.state.settings
    storage = Storage(settings.data_dir)
    source_id = new_id()
    limit = settings.image_max_bytes if source_type == "image" else settings.audio_max_bytes
    try:
        key, size = storage.save(file.file, source_id, limit)
        source = Source(id=source_id, type=source_type)
        source.attachments.append(
            Attachment(
                storage_key=key,
                size_bytes=size,
                original_name=(
                    Path((file.filename or "original").replace("\\", "/")).name or "original"
                )[:255],
                mime_type="application/octet-stream",
            )
        )
        session.execute(text("BEGIN IMMEDIATE"))
        note = Note(
            title=title,
            source=source,
            ingestion_status="queued",
            enrichment_status="not_requested",
            content_version=0,
        )
        session.add(note)
        enqueue(session, note, "extract")
        session.commit()
    except ProcessingError as exc:
        storage.remove_source(source_id)
        raise api_error(
            413 if exc.code == "file_too_large" else 415, exc.code, exc.message
        ) from exc
    except Exception:
        session.rollback()
        storage.remove_source(source_id)
        raise
    finally:
        file.file.close()
    session.refresh(note)
    return NoteDetail.model_validate(note)


@router.post(
    "/reindex",
    response_model=ReindexResponse,
    status_code=202,
    summary="Обработать существующие заметки модулем знаний",
    description="Ставит готовые заметки в очередь после подключения или смены модели/адаптера. "
    "Активные задания той же версии не дублируются. При выключенном адаптере — 409.",
    responses=CONFLICT,
)
def reindex(request: Request, session: Database):
    settings = request.app.state.settings
    if not settings.knowledge_adapter:
        raise api_error(409, "enrichment_disabled", "Модуль обработки знаний не подключён.")
    session.execute(text("BEGIN IMMEDIATE"))
    notes = session.scalars(
        select(Note).where(Note.deleted_at.is_(None), Note.ingestion_status == "ready")
    ).all()
    count = 0
    for note in notes:
        if session.scalar(
            select(Job.id).where(
                Job.note_id == note.id,
                Job.kind == "enrich",
                Job.input_version == note.content_version,
                Job.status.in_(["queued", "processing"]),
            )
        ):
            continue
        queue_enrichment(session, note, settings)
        count += 1
    session.commit()
    return ReindexResponse(queued=count)


@router.get(
    "",
    response_model=NoteList,
    summary="Получить список заметок",
    description="Новые заметки первыми. Полные тексты и оригиналы не включаются. Фильтры комбинируются; удалённые заметки скрыты.",
)
def list_notes(
    session: Database,
    limit: Annotated[int, Query(ge=1, le=100, description="Размер страницы.")] = 20,
    offset: Annotated[int, Query(ge=0, description="Количество пропущенных заметок.")] = 0,
    source_type: SourceType | None = None,
    ingestion_status: IngestionStatus | None = None,
    enrichment_status: EnrichmentStatus | None = None,
):
    conditions = [Note.deleted_at.is_(None)]
    if source_type:
        conditions.append(Source.type == source_type)
    if ingestion_status:
        conditions.append(Note.ingestion_status == ingestion_status)
    if enrichment_status:
        conditions.append(Note.enrichment_status == enrichment_status)
    query = (
        select(Note)
        .join(Source)
        .where(*conditions)
        .options(
            joinedload(Note.source).load_only(Source.type).lazyload(Source.attachments),
            load_only(
                Note.id,
                Note.title,
                Note.ingestion_status,
                Note.enrichment_status,
                Note.summary,
                Note.tags,
                Note.category,
                Note.created_at,
                Note.updated_at,
            ),
        )
        .order_by(Note.created_at.desc(), Note.id.desc())
        .limit(limit)
        .offset(offset)
    )
    notes = session.scalars(query).all()
    total = session.scalar(select(func.count()).select_from(Note).join(Source).where(*conditions))
    return NoteList(
        items=[NoteSummary.model_validate(note) for note in notes],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{note_id}",
    response_model=NoteDetail,
    summary="Получить заметку",
    description="Текст, оригинал, вложения, результаты обогащения и ошибки двух стадий. "
    "Во время обработки возвращается 200. failed относится к стадии, а не HTTP-запросу.",
    responses=NOT_FOUND,
)
def read_note(note_id: UUID, session: Database):
    return NoteDetail.model_validate(get_note(session, note_id))


@router.patch(
    "/{note_id}",
    response_model=NoteDetail,
    summary="Изменить заметку",
    description="Нужны expected_version и хотя бы одно поле: title или extracted_text. null и пустые значения запрещены. "
    "Редактирование доступно после извлечения текста (ready). Оригинал сохраняется. "
    "При изменении версия увеличивается, прежнее обогащение очищается и ставится новое задание. "
    "Устаревший результат обработки не перезапишет новую версию.",
    responses={**NOT_FOUND, **CONFLICT},
)
def edit_note(note_id: UUID, payload: NoteUpdate, request: Request, session: Database):
    session.execute(text("BEGIN IMMEDIATE"))
    note = get_note(session, note_id)
    if note.ingestion_status != "ready":
        raise api_error(409, "note_not_ready", "Сначала дождитесь извлечения текста.")
    if note.content_version != payload.expected_version:
        raise api_error(
            409, "version_conflict", "Заметка уже изменена. Загрузите актуальную версию."
        )
    if (
        payload.extracted_text
        and len(payload.extracted_text) > request.app.state.settings.text_max_chars
    ):
        raise api_error(422, "text_too_large", "Текст превышает настроенный лимит.")
    changed = False
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field != "expected_version" and value != getattr(note, field):
            setattr(note, field, value)
            changed = True
    if changed:
        note.content_version += 1
        note.updated_at = utc_now()
        queue_enrichment(session, note, request.app.state.settings)
    session.commit()
    session.refresh(note)
    return NoteDetail.model_validate(note)


@router.post(
    "/{note_id}/retry",
    response_model=NoteDetail,
    status_code=202,
    summary="Повторить неудачную обработку",
    description="Только для стадии в failed. Оригинал используется повторно. Для ссылки повторное скачивание "
    "нужно только при отсутствии сохранённого HTML. Активные задания повторно не создаются.",
    responses={**NOT_FOUND, **CONFLICT},
)
def retry_note(note_id: UUID, payload: RetryRequest, request: Request, session: Database):
    session.execute(text("BEGIN IMMEDIATE"))
    note = get_note(session, note_id)
    if payload.stage == "enrich" and not request.app.state.settings.knowledge_adapter:
        raise api_error(409, "enrichment_disabled", "Модуль обработки знаний не подключён.")
    prefix = "ingestion" if payload.stage == "extract" else "enrichment"
    if getattr(note, prefix + "_status") != "failed":
        raise api_error(
            409, "invalid_job_state", "Повтор доступен только после ошибки выбранной стадии."
        )
    setattr(note, prefix + "_status", "queued")
    setattr(note, prefix + "_error", None)
    note.updated_at = utc_now()
    enqueue(session, note, payload.stage)
    session.commit()
    session.refresh(note)
    return NoteDetail.model_validate(note)


@router.get(
    "/{note_id}/jobs",
    response_model=list[JobDetail],
    summary="Получить историю обработки заметки",
    description="Последние 100 заданий. После DELETE доступно до завершения очистки, затем — 404. "
    "Ошибка cleanup видна здесь; повторный DELETE повторяет неудачную очистку.",
    responses=NOT_FOUND,
)
def note_jobs(note_id: UUID, session: Database):
    get_note(session, note_id, include_deleted=True)
    jobs = session.scalars(
        select(Job)
        .where(Job.note_id == str(note_id))
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(100)
    )
    return [JobDetail.model_validate(job) for job in jobs]


@router.delete(
    "/{note_id}",
    status_code=204,
    summary="Удалить заметку",
    description="Сразу скрывает заметку и закрывает скачивание оригиналов. Worker удаляет файлы и внешний индекс, "
    "затем записи в базе. Повтор до завершения очистки безопасен и позволяет повторить неудачную очистку. "
    "После завершения повторный DELETE возвращает 404.",
    responses={**NOT_FOUND},
    response_description="Заметка скрыта, очистка поставлена в очередь. Тело отсутствует.",
)
def delete_note(note_id: UUID, session: Database):
    session.execute(text("BEGIN IMMEDIATE"))
    note = get_note(session, note_id, include_deleted=True)
    if not note.deleted_at:
        note.deleted_at = utc_now()
        cancel_pending(session, note.id)
    enqueue(session, note, "cleanup")
    session.commit()
    return Response(status_code=204)
