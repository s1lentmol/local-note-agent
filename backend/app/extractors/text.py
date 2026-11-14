"""Strict UTF-8 (optional BOM) TXT/Markdown; retain all decoded whitespace."""

import unicodedata

from app.errors import ProcessingError

# Only these fixed public errors may cross the compute process boundary.
TEXT_ERRORS = {
    "invalid_encoding": "Ожидается текст в кодировке UTF-8 (допускается BOM).",
    "unsupported_text": "Файл содержит бинарные или недопустимые управляющие символы.",
    "empty_text": "Файл не содержит текста.",
    "text_too_large": "Извлечённый текст превышает допустимое число символов.",
    "file_too_large": "Файл превышает допустимый размер.",
    "original_missing": "Оригинал файла недоступен.",
}


def text_error(code):
    return ProcessingError(code, TEXT_ERRORS[code])


def extract_text(data: bytes, max_chars: int = 100_000) -> str:
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise text_error("invalid_encoding") from None
    if any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in text):
        raise text_error("unsupported_text")
    if not text.strip():
        raise text_error("empty_text")
    if len(text) > max_chars:
        raise text_error("text_too_large")
    return text
