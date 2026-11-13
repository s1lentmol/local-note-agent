from dataclasses import dataclass

from app.model_clients.testing import FakeTextGenerator

VALID = '{"summary":"Plans for Mars", "tags":["space"], "category":"science", "entities":["Mars"]}'


@dataclass(frozen=True)
class GeneratorFactory:
    response: str | None = VALID

    def __call__(self, settings):
        return FakeTextGenerator(structured=[] if self.response is None else [self.response])
