"""Target CRUD routes; prototype API remains separate."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.dto import (
    ErrorResponse,
    NoteDetail,
    NoteListItem,
    NoteListQuery,
    NotePatch,
    Page,
    TextNoteCreate,
)
from app.services.notes import NoteError, NotesService

router = APIRouter(
    prefix="/api/v1/notes",
    tags=["notes"],
    responses={code: {"model": ErrorResponse} for code in (404, 409, 422, 503, 500)},
)


def notes_service(request: Request) -> NotesService:
    service = request.app.state.notes
    if service is None:
        raise NoteError("database_unavailable", "База данных не настроена.")
    return service


Service = Annotated[NotesService, Depends(notes_service)]


@router.post("", status_code=201, response_model=NoteDetail)
def create_note(body: TextNoteCreate, service: Service):
    return service.create(body)


@router.get("", response_model=Page[NoteListItem])
def list_notes(query: Annotated[NoteListQuery, Query()], service: Service):
    return service.list(query)


@router.get("/{note_id}", response_model=NoteDetail)
def get_note(note_id: UUID, service: Service):
    return service.get(note_id)


@router.patch("/{note_id}", response_model=NoteDetail)
def patch_note(note_id: UUID, body: NotePatch, service: Service):
    return service.patch(note_id, body)


@router.delete("/{note_id}", status_code=204)
def delete_note(note_id: UUID, service: Service):
    service.delete(note_id)
    return Response(status_code=204)
