"""Standalone graph endpoint for mounting in the PostgreSQL API."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.dto import ErrorResponse, GraphQuery, GraphResponse
from app.services.graph import GraphService
from app.services.notes import NoteError

router = APIRouter(
    prefix="/api/v1/graph",
    tags=["graph"],
    responses={code: {"model": ErrorResponse} for code in (422, 503, 500)},
)


def graph_service(request: Request) -> GraphService:
    service = getattr(request.app.state, "graph", None)
    if service is None:
        raise NoteError("database_unavailable", "База данных не настроена.")
    return service


@router.get("", response_model=GraphResponse)
def get_graph(
    query: Annotated[GraphQuery, Query()],
    service: Annotated[GraphService, Depends(graph_service)],
):
    return service.get(query)
