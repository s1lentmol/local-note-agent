from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.dto import CollectionCreate, CollectionDetail, CollectionNotesAdd, CollectionSummary
from app.services.collections import CollectionsService
from app.services.notes import NoteError

router = APIRouter(prefix="/api/v1/collections", tags=["collections"])


def collection_service(request: Request):
    service = request.app.state.collections
    if service is None:
        raise NoteError("database_unavailable", "База данных не настроена.")
    return service


Service = Annotated[CollectionsService, Depends(collection_service)]


@router.post("", status_code=201, response_model=CollectionSummary)
def create(body: CollectionCreate, service: Service):
    return service.create(body.title)


@router.get("", response_model=list[CollectionSummary])
def list_collections(service: Service):
    return service.list()


@router.get("/{collection_id}", response_model=CollectionDetail)
def get(
    collection_id: UUID,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return service.get(collection_id, limit, offset)


@router.post("/{collection_id}/notes", status_code=204)
def add(collection_id: UUID, body: CollectionNotesAdd, service: Service):
    service.add(collection_id, body.note_ids)


@router.delete("/{collection_id}/notes/{note_id}", status_code=204)
def remove(collection_id: UUID, note_id: UUID, service: Service):
    service.remove(collection_id, note_id)


@router.delete("/{collection_id}", status_code=204)
def delete(collection_id: UUID, service: Service):
    service.delete(collection_id)
