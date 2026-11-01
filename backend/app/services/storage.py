import os
import shutil
from pathlib import Path
from uuid import uuid4

from app.errors import ProcessingError


class Storage:
    def __init__(self, data_dir: Path):
        self.root = (data_dir / "uploads").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ProcessingError("invalid_storage_path", "Некорректный путь вложения.")
        return path

    def save(self, stream, source_id: str, limit: int) -> tuple[str, int]:
        key = f"{source_id}/original"
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4()}.tmp")
        size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := stream.read(64 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise ProcessingError("file_too_large", "Файл превышает допустимый размер.")
                    output.write(chunk)
                if size == 0:
                    raise ProcessingError("empty_file", "Загружен пустой файл.")
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return key, size

    def remove_source(self, source_id: str):
        path = self.path(source_id)
        if path.exists():
            shutil.rmtree(path)
