from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.notes import Database
from app.errors import api_error
from app.models import Attachment, Note, Source
from app.schemas import ErrorResponse
from app.services.storage import Storage

router = APIRouter(prefix="/api/v1/attachments", tags=["attachments"])


@router.get(
    "/{attachment_id}/download",
    summary="Скачать оригинал материала",
    description="Возвращает исходный файл или сохранённый HTML. Удалённые заметки недоступны сразу после DELETE. "
    "Файлы отдаются как вложение; HTML — с MIME application/octet-stream.",
    response_class=FileResponse,
    responses={
        200: {
            "description": "Бинарный файл.",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        },
        404: {"model": ErrorResponse, "description": "Вложение не найдено или заметка удалена."},
        422: {"model": ErrorResponse, "description": "Некорректный UUID."},
    },
)
def download(attachment_id: UUID, request: Request, session: Database):
    attachment = session.scalar(
        select(Attachment)
        .join(Source)
        .join(Note)
        .where(Attachment.id == str(attachment_id), Note.deleted_at.is_(None))
    )
    if not attachment:
        raise api_error(404, "attachment_not_found", "Вложение не найдено.")
    path = Storage(request.app.state.settings.data_dir).path(attachment.storage_key)
    if not path.is_file():
        raise api_error(404, "attachment_not_found", "Файл вложения не найден.")
    mime = (
        attachment.mime_type
        if attachment.mime_type in ("image/png", "image/jpeg", "audio/wav", "audio/mpeg")
        else "application/octet-stream"
    )
    return FileResponse(
        path,
        filename=attachment.original_name,
        media_type=mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
        },
    )
