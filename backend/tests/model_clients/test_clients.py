import http.client
import math
import socket
import traceback
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from pydantic import Field

from app.model_clients import (
    ASRSettings,
    EmbeddingSettings,
    HTTPTranscriber,
    LLMSettings,
    OllamaEmbedder,
    OpenAITextGenerator,
)
from app.model_clients.contracts import (
    Generation,
    Message,
    ModelClientError,
    StrictModel,
    Tool,
    Transcript,
)
from app.model_clients.http import HTTPTransport
from app.model_clients.native_asr import NativeWhisperTranscriber, clean_transcript
from app.model_clients.testing import FakeEmbedder, FakeTextGenerator, FakeTranscriber


class Args(StrictModel):
    query: str = Field(min_length=1)


class Summary(StrictModel):
    summary: str = Field(min_length=1)
    tags: list[str]


class Transport:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def request(self, settings, path, payload=None, **kwargs):
        self.requests.append((path, payload, kwargs))
        return next(self.responses)


def completion(content="Ответ", calls=None, finish="stop"):
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content, "tool_calls": calls},
                "finish_reason": finish,
            }
        ]
    }


def call(arguments='{"query":"Орбита"}', name="search"):
    return {"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}


def test_chat_tools_and_results():
    transport = Transport(completion(), completion(None, [call()], "tool_calls"), completion())
    client = OpenAITextGenerator(LLMSettings(), transport)
    messages = [Message(role="user", content="Найди Орбиту")]
    assert client.chat(messages).content == "Ответ"
    result = client.chat(messages, [Tool("search", "Поиск", Args)])
    assert result.tool_calls[0].arguments == {"query": "Орбита"}
    client.chat(
        [
            *messages,
            result.as_message(),
            Message(role="tool", tool_call_id="call_1", content="Найдено"),
        ]
    )
    payload = transport.requests[-1][1]
    assert payload["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"query": "Орбита"}'
    assert payload["messages"][-1]["tool_call_id"] == "call_1"
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize(
    "arguments", ["{", "[]", '{"query":3}', '{"query":""}', '{"query":"x","extra":1}']
)
def test_invalid_tool_arguments(arguments):
    client = OpenAITextGenerator(
        LLMSettings(), Transport(completion(None, [call(arguments)], "tool_calls"))
    )
    with pytest.raises(ModelClientError, match="invalid_generation"):
        client.chat([], [Tool("search", "", Args)])


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        completion(" "),
        completion("x", finish="length"),
        completion(None, [call(name="unknown")], "tool_calls"),
        completion(None, [call(), call()], "tool_calls"),
        completion(None, [call()], "stop"),
    ],
)
def test_invalid_generation(response):
    with pytest.raises(ModelClientError, match="invalid_generation"):
        OpenAITextGenerator(LLMSettings(), Transport(response)).chat([], [Tool("search", "", Args)])


def test_json_mode_and_profile_options():
    transport = Transport(completion('{"summary":"Текст","tags":["тег"]}'))
    client = OpenAITextGenerator(
        LLMSettings(options={"reasoning_effort": "none", "seed": 42}), transport
    )
    assert client.json([], Summary).tags == ["тег"]
    payload = transport.requests[0][1]
    assert payload["response_format"] == {"type": "json_object"}
    assert "tools" not in payload
    assert payload["reasoning_effort"] == "none"


@pytest.mark.parametrize(
    "value", ["not json", '{"summary":1,"tags":[]}', '{"summary":"x","tags":[],"extra":1}']
)
def test_invalid_json_mode(value):
    with pytest.raises(ModelClientError, match="invalid_structured_result"):
        OpenAITextGenerator(LLMSettings(), Transport(completion(value))).json([], Summary)


def tags(settings):
    return {"models": [{"name": settings.model, "digest": settings.digest}]}


def test_embeddings_document_query_identity():
    settings = EmbeddingSettings(dimensions=2)
    transport = Transport(
        tags(settings),
        {"model": settings.model, "embeddings": [[0.6, 0.8]]},
        tags(settings),
        {"model": settings.model, "embeddings": [[1.0, 0.0]]},
    )
    client = OllamaEmbedder(settings, transport)
    assert client.documents([" текст  \n заметки "]) == [[0.6, 0.8]]
    assert client.queries(["вопрос"]) == [[1, 0]]
    doc, query = transport.requests[1][1], transport.requests[3][1]
    assert doc["input"] == ["текст заметки"]
    assert doc["truncate"] is False
    assert query["input"] == [f"Instruct: {settings.query_instruction}\nQuery: вопрос"]
    assert settings.digest in client.identity
    assert client.documents([]) == []


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [[1]],
        [[0, 0]],
        [[2, 0]],
        [[float("nan"), 0]],
        [[float("inf"), 0]],
        [[True, 0]],
        [["1", 0]],
        [[1, 0], [1, 0]],
    ],
)
def test_invalid_vectors(vectors):
    settings = EmbeddingSettings(dimensions=2)
    with pytest.raises(ModelClientError, match="invalid_embeddings"):
        OllamaEmbedder(
            settings, Transport(tags(settings), {"model": settings.model, "embeddings": vectors})
        ).documents(["x"])


def test_embedding_model_mismatch():
    settings = EmbeddingSettings(dimensions=2)
    with pytest.raises(ModelClientError, match="embedding_identity_mismatch"):
        OllamaEmbedder(settings, Transport({"models": []})).documents(["x"])
    with pytest.raises(ModelClientError, match="invalid_embeddings"):
        OllamaEmbedder(
            settings, Transport(tags(settings), {"model": "other", "embeddings": [[1, 0]]})
        ).documents(["x"])


@pytest.mark.parametrize(
    "raw",
    [
        {"text": "", "language": "ru"},
        {"text": "  ", "language": "ru"},
        {"text": "x"},
        {"text": "x", "language": "ru", "segments": []},
    ],
)
def test_invalid_asr(tmp_path, raw):
    path = tmp_path / "a.ogg"
    path.write_bytes(b"audio")
    with pytest.raises(ModelClientError, match="invalid_transcript"):
        HTTPTranscriber(ASRSettings(), Transport(raw)).transcribe(path)


def test_asr_success_and_limits(tmp_path):
    path = tmp_path / "a.ogg"
    path.write_bytes(b"audio")
    client = HTTPTranscriber(ASRSettings(), Transport({"text": "Текст", "language": "ru"}))
    assert client.transcribe(path) == Transcript(text="Текст", language="ru")
    with pytest.raises(ModelClientError, match="invalid_audio_size"):
        HTTPTranscriber(ASRSettings(max_bytes=1)).transcribe(path)


def test_native_cleanup_and_parameters(tmp_path, monkeypatch):
    assert (
        clean_transcript(
            "[00:00:00.000 --> 00:00:03.000] Привет\n[00:00:03.000 --> 00:00:05.000] Мир",
            "auto-detected language: ru",
        ).text
        == "Привет\nМир"
    )
    for stdout, stderr in [("", "auto-detected language: ru"), ("x", "")]:
        with pytest.raises(ModelClientError, match="empty_asr_result"):
            clean_transcript(stdout, stderr)
    runtime, model = tmp_path / "cli", tmp_path / "model"
    runtime.touch()
    model.touch()
    monkeypatch.setattr("app.model_clients.native_asr.decode", Mock())
    run = Mock(return_value=Mock(stdout="Текст", stderr="auto-detected language: ru"))
    monkeypatch.setattr("app.model_clients.native_asr.run", run)
    assert NativeWhisperTranscriber(runtime, model).transcribe(Path("x")).language == "ru"
    command = run.call_args.args[0]
    assert "-nt" not in command
    assert command[-4:] == ["-tp", "0", "-tpi", "0"]


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError(), "timeout"),
        (urllib.error.URLError(socket.timeout()), "timeout"),
        (urllib.error.URLError("secret text"), "connection_failed"),
        (urllib.error.HTTPError("http://secret", 503, "private body", {}, None), "http_503"),
    ],
)
def test_transport_safe_errors(error, code):
    transport = HTTPTransport()
    transport.opener = Mock()
    transport.opener.open.side_effect = error
    with pytest.raises(ModelClientError) as exc:
        transport.request(LLMSettings(api_key="secret"), "/chat/completions", {"text": "private"})
    assert str(exc.value) == code
    assert exc.value.__suppress_context__


@pytest.mark.parametrize("stage", ["open", "read"])
@pytest.mark.parametrize(
    "error,code",
    [
        (ConnectionResetError("private network details"), "connection_failed"),
        (BrokenPipeError("private network details"), "connection_failed"),
        (OSError("private network details"), "connection_failed"),
        (http.client.IncompleteRead(b"partial private response", 100), "http_protocol_error"),
        (http.client.BadStatusLine("partial private response"), "http_protocol_error"),
        (http.client.RemoteDisconnected("private network details"), "http_protocol_error"),
        (socket.timeout("private network details"), "timeout"),
        (
            urllib.error.URLError(http.client.BadStatusLine("partial private response")),
            "http_protocol_error",
        ),
        (
            urllib.error.URLError(ConnectionResetError("private network details")),
            "connection_failed",
        ),
    ],
)
def test_transport_connection_and_read_errors(stage, error, code, caplog, capsys):
    transport = HTTPTransport()
    transport.opener = MagicMock()
    response = transport.opener.open.return_value.__enter__.return_value
    if stage == "open":
        transport.opener.open.side_effect = error
    else:
        response.read.side_effect = error
    with pytest.raises(ModelClientError) as exc:
        transport.request(
            LLMSettings(base_url="https://private-host.invalid/v1", api_key="private-key"),
            "/chat/completions",
            {"text": "private input"},
        )
    assert exc.value.code == code
    assert str(exc.value) == code
    assert exc.value.__suppress_context__
    formatted = "".join(traceback.format_exception(exc.value))
    for private in (
        "private-host",
        "private-key",
        "private input",
        "partial private response",
        "private network details",
    ):
        assert private not in formatted
    transport.opener.open.assert_called_once()
    if stage == "read":
        response.read.assert_called_once()
        transport.opener.open.return_value.__exit__.assert_called_once()
    assert not caplog.records
    assert capsys.readouterr() == ("", "")


def test_transport_success_and_invalid_json():
    transport = HTTPTransport()
    transport.opener = Mock()
    transport.opener = MagicMock()
    response = transport.opener.open.return_value.__enter__.return_value
    response.read.return_value = b'{"ok":true}'
    assert transport.request(LLMSettings(), "/x", {}) == {"ok": True}
    response.read.return_value = b"not JSON"
    with pytest.raises(ModelClientError, match="invalid_json"):
        transport.request(LLMSettings(), "/x", {})


def test_settings_and_offline_adapters(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT", "12")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "2")
    monkeypatch.setenv("ASR_TIMEOUT", "42")
    assert LLMSettings.from_env().timeout == 12
    assert EmbeddingSettings.from_env().dimensions == 2
    assert ASRSettings.from_env().timeout == 42
    assert "secret" not in repr(LLMSettings(api_key="secret"))
    fake = FakeTextGenerator(
        [Generation(content="Ответ", finish_reason="stop")], ['{"summary":"x","tags":[]}']
    )
    assert fake.chat([]).content == "Ответ"
    assert fake.json([], Summary).summary == "x"
    embeddings = FakeEmbedder({("document", "x"): [1, 0], ("query", "x"): [0, 1]}, 2)
    assert math.hypot(*embeddings.documents(["x"])[0]) == 1
    assert embeddings.queries(["x"]) == [[0, 1]]
    assert (
        FakeTranscriber(Transcript(text="x", language="en")).transcribe(Path("missing")).text == "x"
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"choices": [None]},
        {"choices": [{"message": "bad", "finish_reason": "stop"}]},
        completion(None, [call(arguments='{"query":true}')], "tool_calls"),
    ],
)
def test_malformed_response_shapes(raw):
    with pytest.raises(ModelClientError, match="invalid_generation"):
        OpenAITextGenerator(LLMSettings(), Transport(raw)).chat([], [Tool("search", "", Args)])


def test_native_runtime_errors(monkeypatch):
    import subprocess

    from app.model_clients.native_asr import run

    for error, code in [
        (subprocess.TimeoutExpired("private filename", 1), "asr_runtime_timeout"),
        (OSError("private filename"), "asr_runtime_unavailable"),
    ]:
        monkeypatch.setattr("subprocess.run", Mock(side_effect=error))
        with pytest.raises(ModelClientError, match=code):
            run(["cli", "private filename"], 1)
    monkeypatch.setattr("subprocess.run", Mock(return_value=Mock(returncode=1)))
    with pytest.raises(ModelClientError, match="asr_runtime_failed"):
        run(["cli"], 1)


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"streams": [], "format": {"format_name": "ogg"}},
        {
            "streams": [{"codec_type": "audio", "codec_name": "vorbis"}],
            "format": {"format_name": "ogg"},
        },
    ],
)
def test_decoder_rejects_unsupported_content(tmp_path, monkeypatch, metadata):
    import json

    from app.model_clients.native_asr import decode

    monkeypatch.setattr(
        "app.model_clients.native_asr.run", Mock(return_value=Mock(stdout=json.dumps(metadata)))
    )
    with pytest.raises(ModelClientError, match="invalid_audio_format"):
        decode(tmp_path / "fake.ogg", tmp_path / "decoded.wav", 1, 1800)


def test_decoder_checks_actual_duration(tmp_path, monkeypatch):
    import json
    import wave

    from app.model_clients.native_asr import decode

    metadata = {
        "streams": [{"codec_type": "audio", "codec_name": "opus"}],
        "format": {"format_name": "ogg"},
    }
    target = tmp_path / "decoded.wav"
    with wave.open(str(target), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 32000)
    monkeypatch.setattr(
        "app.model_clients.native_asr.run", Mock(return_value=Mock(stdout=json.dumps(metadata)))
    )
    with pytest.raises(ModelClientError, match="invalid_audio_duration"):
        decode(tmp_path / "anything.txt", target, 1, 1)
    decode(tmp_path / "anything.txt", target, 1, 3)
