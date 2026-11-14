"""Bounded multipart upload and extraction progress."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.dto import BatchImportFields, ImportAccepted, ImportDetail
from app.errors import ProcessingError
from app.services.imports import MAX_BATCH_BYTES, MAX_FILES, ImportFile
from app.services.notes import NoteError

router = APIRouter(prefix="/api/v1/imports", tags=["imports"])
MAX_BODY_BYTES = MAX_BATCH_BYTES + 128 * 1024


class BatchMultipartParser(MultiPartParser):
    complete = False

    def on_end(self):
        self.complete = True


def import_service(request):
    service = request.app.state.imports
    if service is None:
        raise NoteError("database_unavailable", "База данных не настроена.")
    return service


async def bounded_body(request):
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise ProcessingError("batch_too_large", "Запрос импорта превышает допустимый размер.")
        yield chunk


@router.post(
    "",
    status_code=202,
    response_model=ImportAccepted,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": MAX_FILES,
                                "items": {"type": "string", "format": "binary"},
                            },
                            "collection_id": {"type": "string", "format": "uuid"},
                            "collection_title": {"type": "string", "maxLength": 200},
                        },
                    },
                }
            },
        }
    },
)
async def accept(request: Request):
    service = import_service(request)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "multipart/form-data":
        raise ProcessingError("invalid_upload", "Отправьте файлы в формате multipart/form-data.")
    parser = BatchMultipartParser(
        request.headers,
        bounded_body(request),
        max_files=MAX_FILES,
        max_fields=2,
        max_part_size=2048,
    )
    try:
        form = await parser.parse()
    except MultiPartException:
        raise ProcessingError(
            "invalid_upload", f"Некорректная форма или превышен лимит в {MAX_FILES} файлов."
        ) from None
    try:
        if not parser.complete:
            raise ProcessingError("invalid_upload", "Загрузка файлов прервана.")
        inputs = []
        values = {}
        for key, value in form.multi_items():
            if key == "files" and isinstance(value, UploadFile):
                inputs.append(ImportFile(value.filename, value.size, value.file))
            elif (
                key in {"collection_id", "collection_title"}
                and key not in values
                and isinstance(value, str)
            ):
                values[key] = value
            else:
                raise ProcessingError("invalid_upload", "Некорректные поля формы импорта.")
        try:
            fields = BatchImportFields.model_validate(values)
        except ValidationError:
            raise ProcessingError(
                "invalid_collection", "Укажите ID или название подборки."
            ) from None
        return await run_in_threadpool(service.accept, inputs, fields)
    finally:
        await form.close()


@router.get("/{batch_id}", response_model=ImportDetail)
def get(batch_id: UUID, request: Request):
    return import_service(request).get(batch_id)
