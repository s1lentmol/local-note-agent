"""Explicit real smoke using only synthetic text and public P03 audio.

Exclusive evidence creation: previous runs are never overwritten.
"""

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field

from . import (
    ASRSettings,
    EmbeddingSettings,
    HTTPTranscriber,
    LLMSettings,
    OllamaEmbedder,
    OpenAITextGenerator,
)
from .contracts import Message, StrictModel, Tool


class SearchArgs(StrictModel):
    query: str = Field(min_length=1)


class Enrichment(StrictModel):
    summary: str = Field(min_length=1)
    tags: list[str] = Field(min_length=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["generation", "embeddings", "asr"])
    parser.add_argument("--audio", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"phase": args.phase, "utc": datetime.now(timezone.utc).isoformat(), "runs": []}
    # Claim the output before any network/model work.
    with args.output.open("x", encoding="utf-8") as output:

        def timed(name, action):
            started = time.perf_counter()
            result = action()
            report["runs"].append(
                {"name": name, "seconds": time.perf_counter() - started, "result": result}
            )
            return result

        try:
            if args.phase == "generation":
                settings = LLMSettings.from_env()
                report["profile"] = settings.model_dump(mode="json", exclude={"api_key"})
                client = OpenAITextGenerator(settings)
                timed(
                    "russian",
                    lambda: client.chat(
                        [
                            Message(
                                role="user",
                                content="Зачем нужны резервные копии заметок? Ответь по-русски двумя предложениями.",
                            )
                        ]
                    ).model_dump(),
                )
                messages = [
                    Message(
                        role="system",
                        content="Используй search_notes для поиска. После результата ответь по-русски.",
                    ),
                    Message(
                        role="user",
                        content="Найди встречу по проекту Орбита: когда она и что подготовить?",
                    ),
                ]
                tool = Tool("search_notes", "Поиск заметки по запросу", SearchArgs)
                first = client.chat(messages, [tool])
                if not first.tool_calls:
                    raise RuntimeError("native_tool_call_missing")
                report["runs"].append({"name": "native_tools", "result": first.model_dump()})
                messages.append(first.as_message())
                for call in first.tool_calls:
                    messages.append(
                        Message(
                            role="tool",
                            tool_call_id=call.id,
                            content='{"text":"Встреча по проекту Орбита 15 мая в 10:00. Подготовить макет."}',
                        )
                    )
                timed("tool_result", lambda: client.chat(messages).model_dump())
                timed(
                    "json_mode",
                    lambda: client.json(
                        [
                            Message(
                                role="system",
                                content="Верни JSON с summary (строка) и tags (массив непустых строк), без других полей.",
                            ),
                            Message(
                                role="user",
                                content="Встреча по учебному проекту 15 мая. Нужно подготовить макет.",
                            ),
                        ],
                        Enrichment,
                    ).model_dump(),
                )
            elif args.phase == "embeddings":
                settings = EmbeddingSettings.from_env()
                client = OllamaEmbedder(settings)
                report["profile"] = settings.model_dump(mode="json", exclude={"api_key"})
                documents = timed(
                    "documents",
                    lambda: client.documents(
                        ["Встреча по проекту Орбита 15 мая.", "Резервное копирование базы заметок."]
                    ),
                )
                queries = timed("queries", lambda: client.queries(["Когда встреча Орбита?"]))
                report["identity"] = client.identity
                report["similarities"] = [
                    sum(x * y for x, y in zip(v, queries[0], strict=True)) for v in documents
                ]
            else:
                if not args.audio:
                    raise ValueError("Provide public P03 audio with --audio")
                settings = ASRSettings.from_env()
                report["profile"] = settings.model_dump(mode="json", exclude={"api_key"})
                client = HTTPTranscriber(settings)
                for audio in args.audio:
                    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
                    timed(
                        audio.suffix,
                        lambda: {"sha256": digest, **client.transcribe(audio).model_dump()},
                    )
            report["status"] = "pass"
        except Exception as exc:
            # Never store raw upstream errors or payloads on failure.
            report["status"] = "failed"
            report["error"] = getattr(exc, "code", type(exc).__name__)
            raise
        finally:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")


if __name__ == "__main__":
    main()
