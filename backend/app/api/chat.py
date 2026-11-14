from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.dto import ChatRequest, ChatResponse, ErrorResponse
from app.services.chat import ChatError, ChatService

router = APIRouter(
    prefix="/api/v1/chat",
    tags=["chat"],
    responses={code: {"model": ErrorResponse} for code in (404, 409, 422, 503, 500)},
)


def chat_service(request: Request) -> ChatService:
    service = request.app.state.chat
    if service is None:
        raise ChatError("database_unavailable", "База данных не настроена.", 503)
    return service


@router.post("/messages", response_model=ChatResponse)
def reply(body: ChatRequest, service: Annotated[ChatService, Depends(chat_service)]):
    return service.reply(body)
