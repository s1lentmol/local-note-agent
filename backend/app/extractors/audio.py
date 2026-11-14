"""Validate a stored original, then call the T07 transcription adapter."""

from fractions import Fraction

import av

from app.errors import ProcessingError
from app.extractors.text import text_error
from app.model_clients.contracts import ModelClientError, Transcriber

AUDIO_ERRORS = {
    "invalid_audio": "Аудиофайл повреждён или имеет неподдерживаемый формат. Ожидается OGG/Opus, WAV (PCM) или MP3.",
    "audio_too_long": "Аудио превышает допустимую длительность.",
    "asr_unavailable": "Сервис распознавания речи недоступен. Повторите извлечение позже.",
    "asr_timeout": "Истекло время ожидания распознавания речи.",
    "empty_asr_result": "Распознавание не вернуло текст.",
}
ASR_ERRORS = {
    "invalid_audio_size": "file_too_large",
    "http_413": "file_too_large",
    "invalid_audio_format": "invalid_audio",
    "invalid_audio_duration": "audio_too_long",
    "invalid_audio_data": "invalid_audio",
    "empty_asr_result": "empty_asr_result",
    "timeout": "asr_timeout",
    "asr_runtime_timeout": "asr_timeout",
}


def audio_error(code):
    return ProcessingError(code, AUDIO_ERRORS[code])


def check_ogg_pages(source, size):
    # The demuxer can return a valid prefix at EOF after a truncated Ogg page.
    eos = False
    while source.tell() < size:
        header = source.read(27)
        if len(header) != 27 or header[:5] != b"OggS\0" or eos:
            raise audio_error("invalid_audio")
        lengths = source.read(header[26])
        if len(lengths) != header[26]:
            raise audio_error("invalid_audio")
        source.seek(sum(lengths), 1)
        eos = bool(header[5] & 4)
    if source.tell() != size or not eos:
        raise audio_error("invalid_audio")


def validate_audio(path, max_bytes, max_seconds):
    size = path.stat().st_size
    if size > max_bytes:
        raise text_error("file_too_large")
    try:
        with (
            path.open("rb") as source,
            av.open(
                source, options={"format_whitelist": "ogg,wav,mp3", "err_detect": "explode"}
            ) as container,
        ):
            streams = container.streams.audio
            if len(streams) != 1:
                raise audio_error("invalid_audio")
            stream = streams[0]
            codec = stream.codec_context.codec.canonical_name
            format_name = container.format.name
            if not (
                (format_name == "ogg" and codec == "opus")
                or (format_name == "wav" and codec.startswith("pcm_"))
                or (format_name == "mp3" and codec == "mp3")
            ):
                raise audio_error("invalid_audio")
            stream.codec_context.options = {"err_detect": "explode"}
            duration = Fraction(0)
            for packet in container.demux(stream):
                if packet.is_corrupt:
                    raise audio_error("invalid_audio")
                for frame in packet.decode():
                    duration += Fraction(frame.samples, frame.sample_rate)
                    if duration > max_seconds:
                        raise audio_error("audio_too_long")
            if not duration:
                raise audio_error("invalid_audio")
            if format_name == "ogg":
                source.seek(0)
                check_ogg_pages(source, size)
    except av.FFmpegError:
        raise audio_error("invalid_audio") from None


def transcribe_audio(path, transcriber: Transcriber, max_chars):
    try:
        transcript = transcriber.transcribe(path)
    except ModelClientError as exc:
        code = ASR_ERRORS.get(exc.code, "asr_unavailable")
        if code == "file_too_large":
            raise text_error(code) from None
        raise audio_error(code) from None
    if not transcript.text.strip():
        raise audio_error("empty_asr_result")
    if len(transcript.text) > max_chars:
        raise text_error("text_too_large")
    if "\x00" in transcript.text:
        raise audio_error("asr_unavailable")
    return transcript
