import pytest

from app.errors import ProcessingError
from app.extractors.text import extract_text


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf"])
def test_utf8_markdown_preserves_layout(prefix):
    text = "# Заголовок\r\n\r\n  Абзац\n\n- пункт\n"
    assert extract_text(prefix + text.encode()) == text


@pytest.mark.parametrize(
    "data,code",
    [
        (b"", "empty_text"),
        (b" \r\n\t", "empty_text"),
        (b"\xef\xbb\xbf", "empty_text"),
        (b"\xff", "invalid_encoding"),
        ("текст".encode("cp1251"), "invalid_encoding"),
        (b"PK\x03\x04", "unsupported_text"),
        (b"a\x00b", "unsupported_text"),
        (b"a" * 100001, "text_too_large"),
    ],
)
def test_rejections(data, code):
    with pytest.raises(ProcessingError) as exc:
        extract_text(data)
    assert exc.value.code == code


def test_exact_character_limit():
    assert len(extract_text(("я" * 100000).encode())) == 100000
