from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, lazyload, load_only

from app.models import Note, Source, utc_now
from app.schemas import ErrorResponse, NoteCreate, NoteDetail, NoteList, NoteSummary, NoteUpdate

router = APIRouter(prefix="/api/v1/notes", tags=["notes"], responses={
    422: {"model": ErrorResponse, "description": "Некорректные поля запроса или параметры."},
})
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Заметка не найдена."}}


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


Database = Annotated[Session, Depends(get_session)]


def get_note(session: Session, note_id: UUID) -> Note:
    note = session.get(Note, str(note_id))
    if note is None:
        raise HTTPException(404, {"code": "note_not_found", "message": "Заметка не найдена."})
    return note


def version_conflict() -> HTTPException:
    return HTTPException(409, {
        "code": "version_conflict",
        "message": "Заметка уже изменена. Загрузите актуальную версию.",
    })


@router.post(
    "", response_model=NoteDetail, status_code=201,
    summary="Создать текстовую заметку",
    description="Сохраняет исходный текст и его редактируемую копию. Начальная версия — 1.",
    response_description="Созданная заметка.",
)
def create_note(payload: NoteCreate, session: Database):
    note = Note(
        title=payload.title,
        extracted_text=payload.text,
        source=Source(original_text=payload.text),
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return NoteDetail.model_validate(note)


@router.get(
    "", response_model=NoteList, summary="Получить список заметок",
    description="Возвращает заметки от новых к старым, без полных текстов и источников.",
    response_description="Страница заметок и их общее количество.",
)
def list_notes(
    session: Database,
    limit: Annotated[int, Query(ge=1, le=100, description="Размер страницы, от 1 до 100.")] = 20,
    offset: Annotated[int, Query(ge=0, description="Количество заметок, которые нужно пропустить.")] = 0,
):
    notes = session.scalars(
        select(Note)
        .options(
            lazyload(Note.source),
            load_only(Note.id, Note.title, Note.created_at, Note.updated_at),
        )
        .order_by(Note.created_at.desc(), Note.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    total = session.scalar(select(func.count()).select_from(Note))
    return NoteList(
        items=[NoteSummary.model_validate(note) for note in notes],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{note_id}", response_model=NoteDetail, summary="Получить заметку",
    description="Возвращает текущий текст, оригинал и версию заметки по её UUID.",
    responses=NOT_FOUND,
)
def read_note(note_id: UUID, session: Database):
    return NoteDetail.model_validate(get_note(session, note_id))


@router.patch(
    "/{note_id}", response_model=NoteDetail, summary="Изменить заметку",
    description=(
        "Передайте expected_version и хотя бы одно поле: title или extracted_text. "
        "Пустые значения и null не допускаются. Исходный текст сохраняется. "
        "При изменении содержимого версия увеличивается; при одинаковых значениях остаётся прежней."
    ),
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "Версия заметки уже изменилась."}},
)
def edit_note(note_id: UUID, payload: NoteUpdate, session: Database):
    note = get_note(session, note_id)
    if note.content_version != payload.expected_version:
        raise version_conflict()

    changes = {
        field: value
        for field, value in payload.model_dump(exclude_unset=True).items()
        if field != "expected_version" and value != getattr(note, field)
    }
    if changes:
        result = session.execute(
            update(Note)
            .where(Note.id == str(note_id), Note.content_version == payload.expected_version)
            .values(**changes, content_version=Note.content_version + 1, updated_at=utc_now())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            session.rollback()
            raise version_conflict()
        session.commit()
        session.refresh(note)
    return NoteDetail.model_validate(note)


@router.delete(
    "/{note_id}", status_code=204, summary="Удалить заметку",
    description="Удаляет заметку и её исходный текст из базы. Восстановление не предусмотрено.",
    response_description="Заметка удалена. Тело ответа отсутствует.",
    responses=NOT_FOUND,
)
def delete_note(note_id: UUID, session: Database):
    session.delete(get_note(session, note_id))
    session.commit()
    return Response(status_code=204)
