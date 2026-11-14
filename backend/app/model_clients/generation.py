import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import Generation, Message, ModelClientError, ToolCall
from .http import HTTPTransport


class WireModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class WireFunction(WireModel):
    name: str
    arguments: str


class WireCall(WireModel):
    id: str = Field(min_length=1)
    type: Literal["function"]
    function: WireFunction


class WireMessage(WireModel):
    role: Literal["assistant"]
    content: str | None = None
    tool_calls: list[WireCall] | None = None


class WireChoice(WireModel):
    message: WireMessage
    finish_reason: Literal["stop", "tool_calls"]


class WireResponse(WireModel):
    choices: list[WireChoice] = Field(min_length=1, max_length=1)


def wire(message: Message):
    result = {"role": message.role, "content": message.content}
    if message.tool_call_id:
        result["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        result["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.name,
                    "arguments": json.dumps(c.arguments, ensure_ascii=False),
                },
            }
            for c in message.tool_calls
        ]
    return result


class OpenAITextGenerator:
    def __init__(self, settings, transport=None, *, validate_tool_arguments=True):
        self.settings, self.transport = settings, transport or HTTPTransport()
        self.validate_tool_arguments = validate_tool_arguments

    def _generate(self, messages, tools=(), json_mode=False):
        payload = {
            "model": self.settings.model,
            "messages": [wire(m) for m in messages],
            "stream": False,
            "max_tokens": self.settings.max_tokens,
            **self.settings.options,
        }
        by_name = {t.name: t for t in tools}
        if len(by_name) != len(tools):
            raise ValueError("Duplicate tool names")
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.arguments.model_json_schema(),
                    },
                }
                for t in tools
            ]
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        raw = self.transport.request(self.settings, "/chat/completions", payload)
        try:
            choice = WireResponse.model_validate(raw).choices[0]
            msg = choice.message
            calls = []
            ids = set()
            for call in msg.tool_calls or []:
                function = call.function
                if call.type != "function" or function.name not in by_name or call.id in ids:
                    raise ValueError()
                if self.validate_tool_arguments:
                    args = (
                        by_name[function.name]
                        .arguments.model_validate_json(function.arguments, strict=True)
                        .model_dump(mode="json")
                    )
                else:
                    # Application validates the decoded object and can return a tool error.
                    args = json.loads(function.arguments)
                calls.append(ToolCall(id=call.id, name=function.name, arguments=args))
                ids.add(call.id)
            content = msg.content
            if not calls and (not isinstance(content, str) or not content.strip()):
                raise ValueError()
            if bool(calls) != (choice.finish_reason == "tool_calls"):
                raise ValueError()
            return Generation(content=content, tool_calls=calls, finish_reason=choice.finish_reason)
        except (KeyError, TypeError, ValueError, ValidationError):
            raise ModelClientError("invalid_generation") from None

    def chat(self, messages, tools=()):
        return self._generate(messages, tools)

    def json(self, messages, schema):
        result = self._generate(messages, json_mode=True)
        try:
            return schema.model_validate_json(result.content, strict=True)
        except (ValidationError, ValueError, TypeError):
            raise ModelClientError("invalid_structured_result") from None
