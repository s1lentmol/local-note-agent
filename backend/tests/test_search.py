import pytest
from pydantic import ValidationError

from app.services.search import SearchSettings


def test_search_settings_environment(monkeypatch):
    monkeypatch.setenv("NOTE_AGENT_SEARCH_DENSE_ENABLED", "false")
    monkeypatch.setenv("NOTE_AGENT_SEARCH_LEXICAL_CANDIDATES", "150")
    monkeypatch.setenv("NOTE_AGENT_SEARCH_DENSE_CANDIDATES", "200")
    monkeypatch.setenv("NOTE_AGENT_SEARCH_RRF_K", "30")
    monkeypatch.setenv("NOTE_AGENT_SEARCH_PREVIEW_CHARS", "90")
    assert SearchSettings.from_env() == SearchSettings(
        dense_enabled=False,
        lexical_candidates=150,
        dense_candidates=200,
        rrf_k=30,
        preview_chars=90,
    )


@pytest.mark.parametrize(
    "values",
    [
        {"lexical_candidates": 0},
        {"dense_candidates": 10001},
        {"rrf_k": 0},
        {"preview_chars": 0},
        {"dense_enabled": "maybe"},
    ],
)
def test_invalid_search_settings(values):
    with pytest.raises(ValidationError):
        SearchSettings(**values)
