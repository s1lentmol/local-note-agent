"""Standalone search requires embeddings only when dense is explicitly enabled."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.dto import ErrorResponse, SearchRequest, SearchResponse
from app.services.notes import NoteError
from app.services.search import SearchService

router = APIRouter(
    prefix="/api/v1/search",
    tags=["search"],
    responses={code: {"model": ErrorResponse} for code in (422, 503, 500)},
)


def search_service(request: Request) -> SearchService:
    service = request.app.state.search
    if service is None:
        raise NoteError("database_unavailable", "База данных не настроена.")
    return service


@router.post("", response_model=SearchResponse)
def search(body: SearchRequest, service: Annotated[SearchService, Depends(search_service)]):
    return service.search(body)
