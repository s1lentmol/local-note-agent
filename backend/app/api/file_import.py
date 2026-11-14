"""PostgreSQL single-file upload and original download routes."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.responses import FileResponse

from app.dto import FileImportFields, NoteDetail
from app.errors import ProcessingError
from app.services.file_import import FileImportService
from app.services.notes import NoteError

router = APIRouter(prefix="/api/v1", tags=["files"])


def file_service(request: Request):
    service = request.app.state.files
    if service is None:
        raise NoteError("database_unavailable", "База данных не настроена.")
    return service


Service = Annotated[FileImportService, Depends(file_service)]


async def single_upload(request: Request):
    form = await request.form()
    uploads = [value for _, value in form.multi_items() if isinstance(value, StarletteUploadFile)]
    if len(uploads) != 1 or len(form.getlist("file")) != 1:
        raise ProcessingError("invalid_upload", "Загрузите ровно один файл в поле file.")


@router.post(
    "/notes/import",
    status_code=202,
    response_model=NoteDetail,
    dependencies=[Depends(single_upload)],
)
def import_note(
    service: Service,
    file: Annotated[UploadFile, File()],
    source_type: Annotated[Literal["text_file", "audio"], Form()],
    title: Annotated[str | None, Form(max_length=200, min_length=1)] = None,
):
    # FileImportFields also rejects whitespace-only titles.
    try:
        fields = FileImportFields(source_type=source_type, title=title)
    except ValidationError:
        raise ProcessingError("invalid_title", "Некорректный заголовок.") from None
    return service.import_file(file.file, file.filename, fields)


@router.get("/attachments/{attachment_id}/download")
def download(attachment_id: UUID, service: Service):
    stream, record = service.download(attachment_id)
    # Reuse Starlette's safe RFC 5987 filename encoding. Never expose storage_key.
    headers = dict(FileResponse("unused", filename=record["original_name"]).headers)
    return StreamingResponse(
        stream,
        media_type=record["mime_type"],
        headers={
            "Content-Disposition": headers["content-disposition"],
            "Content-Length": str(record["size_bytes"]),
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(stream.close),
    )
