"""Real, non-streaming HTTP smoke checks; all notes below are synthetic."""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field


class SearchArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    query: str = Field(min_length=1, max_length=100, pattern=r"\S")


class Enrichment(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    summary: str = Field(min_length=1)
    tags: list[str] = Field(min_length=1)


NOTES = (
    {"id": "demo-1", "text": "Встреча по проекту Орбита состоится 15 мая в 10:00. Нужно подготовить макет."},
    {"id": "demo-2", "text": "Резервную копию учебных заметок следует делать каждую пятницу."},
)
TOOLS = [{"type": "function", "function": {
    "name": "search_notes",
    "description": "Поиск по двум демонстрационным заметкам по подстроке без учёта регистра.",
    "parameters": SearchArgs.model_json_schema(),
}}]


def search_notes(args: SearchArgs) -> list[dict]:
    return [note for note in NOTES if args.query.casefold() in note["text"].casefold()]


def russian_answer(message: dict) -> str:
    content = message.get("content")
    if not isinstance(content, str) or len(re.findall(r"[А-Яа-яЁё]", content)) < 30:
        raise ValueError("Expected a substantive Russian answer")
    return content


def run(output: Path) -> None:
    base_url = os.environ["BASE_URL"].rstrip("/")
    model = os.environ["MODEL"]
    api_key = os.environ["API_KEY"]
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(),
                "base_url": base_url, "model": model, "requests": [], "passed": []}
    # Explicitly bypass proxy environment for the local experiment.
    opener = build_opener(ProxyHandler({}))

    def chat(label: str, messages: list[dict], **options) -> dict:
        payload = {"model": model, "messages": messages, "stream": False,
                   "temperature": 0, "seed": 42, "max_tokens": 512,
                   "reasoning_effort": "none", **options}
        record = {"label": label, "request": payload}
        evidence["requests"].append(record)
        request = Request(base_url + "/chat/completions",
                          data=json.dumps(payload, ensure_ascii=False).encode(),
                          headers={"Content-Type": "application/json",
                                   "Authorization": "Bearer " + api_key})
        start = time.perf_counter()
        with opener.open(request, timeout=180) as response:
            record["http_status"] = response.status
            body = json.load(response)
        record["seconds"] = round(time.perf_counter() - start, 3)
        record["response"] = body
        choice = body["choices"][0]
        if choice["finish_reason"] not in ("stop", "tool_calls"):
            raise ValueError(f"Incomplete response: {choice['finish_reason']}")
        print(label, record["seconds"], json.dumps(choice["message"], ensure_ascii=False))
        return choice["message"]

    try:
        message = chat("russian", [{"role": "user", "content":
            "Объясни по-русски в двух предложениях, зачем делать резервные копии заметок."}])
        russian_answer(message)
        evidence["passed"].append("russian")

        messages = [{"role": "system", "content":
            "Отвечай по-русски. Для фактов из заметок используй search_notes; не придумывай их. "
            "Ищи по слову Орбита. После результата инструмента дай краткий ответ без новых вызовов."},
            {"role": "user", "content": "Когда встреча по проекту Орбита и что нужно подготовить? Проверь заметки."}]
        tool_seen = False
        for attempt in range(4):
            message = chat(f"tools-{attempt + 1}", messages, tools=TOOLS)
            calls = message.get("tool_calls")
            if not calls:
                if not tool_seen:
                    raise ValueError("No native tool_calls: text imitation does not count")
                content = russian_answer(message)
                if not all(value in content for value in ("15 мая", "10:00", "макет")):
                    raise ValueError("Final answer omitted expected facts")
                evidence["passed"].append("native_tools_full_cycle")
                break
            if len(calls) > 4:
                raise ValueError("Too many tool calls in one response")
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
            for call in calls:
                if call.get("type") != "function" or call["function"]["name"] != "search_notes":
                    raise ValueError("Tool is not allowed")
                if not isinstance(call.get("id"), str) or not call["id"]:
                    raise ValueError("Missing native tool call id")
                args = SearchArgs.model_validate_json(call["function"]["arguments"])
                result = search_notes(args)
                tool_seen = True
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
        else:
            raise ValueError("Tool loop exceeded four model requests")

        message = chat("json-mode", [{"role": "user", "content":
            "Сделай JSON-объект с summary (строка по-русски) и tags (непустой список строк). "
            "Текст: Встреча по учебному проекту состоится 15 мая. Нужно подготовить макет."}],
            response_format={"type": "json_object"})
        parsed = Enrichment.model_validate_json(message["content"])
        evidence["validated_json"] = parsed.model_dump()
        evidence["passed"].append("api_json_mode_pydantic")
    except Exception as error:
        evidence["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
