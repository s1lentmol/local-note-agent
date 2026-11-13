from dataclasses import replace

import pytest

from app.indexing import ChunkSettings, build_index, split_text
from app.model_clients.contracts import ModelClientError
from app.model_clients.settings import EmbeddingSettings
from app.repositories.index import SpaceProfile
from tests.indexing_fixtures import FixtureEmbedder, failing_embedder, unit_vector


def test_chunk_offsets_overlap_and_small_text():
    text = "абвг дежз иклм нопр стуф"
    assert split_text(text, ChunkSettings(3, 1)) == ["абвг дежз иклм", "иклм нопр стуф"]
    assert split_text("  小 текст.\n", ChunkSettings()) == ["小 текст."]
    assert split_text(" \n\t", ChunkSettings()) == []
    assert split_text("x" * 25, ChunkSettings(3, 0)) == ["x" * 12, "x" * 12, "x"]


@pytest.mark.parametrize("args", [(0, 0), (3, 3), (3, -1), (3, 1, 0)])
def test_invalid_chunk_settings(args):
    with pytest.raises(ValueError):
        ChunkSettings(*args)


def test_index_batches_title_identity():
    settings = EmbeddingSettings()
    embedder = FixtureEmbedder(settings)
    profile = SpaceProfile.from_settings(settings)
    result = build_index(
        "Mars", "abcd efgh ijkl mnop qrst", embedder, profile, ChunkSettings(2, 0, 2)
    )
    assert result.chunks == ["abcd efgh", "ijkl mnop", "qrst"]
    assert embedder.requests == [["Mars\n\nabcd efgh", "Mars\n\nijkl mnop"], ["Mars\n\nqrst"]]
    assert result.vectors == [unit_vector()] * 3
    with pytest.raises(ValueError):
        build_index("t", "text", embedder, replace(profile, model="different"), ChunkSettings())
    with pytest.raises(ValueError):
        build_index("t", "text", embedder, replace(profile, dimensions=2), ChunkSettings())


@pytest.mark.parametrize(
    "value", [[0.0] * 1024, [1.0] * 1024, [float("nan")] * 1024, [float("inf")] * 1024, [1.0], []]
)
def test_invalid_model_vectors(value):
    settings = EmbeddingSettings()
    embedder = FixtureEmbedder(settings)
    embedder.documents = lambda _: [value]
    with pytest.raises(ValueError):
        build_index("t", "text", embedder, SpaceProfile.from_settings(settings), ChunkSettings())


def test_model_failure_and_result_count():
    settings = EmbeddingSettings()
    profile = SpaceProfile.from_settings(settings)
    with pytest.raises(ModelClientError):
        build_index("t", "text", failing_embedder(settings), profile, ChunkSettings())
    embedder = FixtureEmbedder(settings)
    embedder.documents = lambda _: []
    with pytest.raises(ValueError):
        build_index("t", "text", embedder, profile, ChunkSettings())
