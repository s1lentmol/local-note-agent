import warnings
from pathlib import Path

import av
from PIL import Image, ImageOps, UnidentifiedImageError

from app.errors import ProcessingError


def validate_image(path: Path, settings) -> tuple[str, dict]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in ("JPEG", "PNG"):
                    raise ProcessingError(
                        "unsupported_file_type", "Поддерживаются изображения JPEG и PNG."
                    )
                if image.width * image.height > settings.image_max_pixels:
                    raise ProcessingError(
                        "image_too_large", "Слишком большое разрешение изображения."
                    )
                mime = Image.MIME[image.format]
                metadata = {"width": image.width, "height": image.height}
                image.verify()
        return mime, metadata
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ProcessingError("invalid_image", "Не удалось прочитать изображение.") from exc


def validate_audio(path: Path, settings) -> tuple[str, dict]:
    try:
        with av.open(str(path)) as container:
            formats = container.format.name.split(",")
            if not set(formats) & {"wav", "mp3"} or not container.streams.audio:
                raise ProcessingError(
                    "unsupported_file_type", "Поддерживаются аудиофайлы WAV и MP3."
                )
            # Count decoded samples, rather than trusting duration metadata.
            duration = 0.0
            for frame in container.decode(audio=0):
                duration += frame.samples / frame.sample_rate
                if duration > settings.audio_max_seconds:
                    raise ProcessingError(
                        "audio_too_long", "Аудио превышает допустимую длительность."
                    )
            if duration == 0:
                raise ProcessingError("invalid_audio", "В файле нет аудиоданных.")
            return ("audio/wav" if "wav" in formats else "audio/mpeg"), {
                "duration_seconds": round(duration, 3)
            }
    except (av.FFmpegError, OSError, ValueError, ZeroDivisionError) as exc:
        raise ProcessingError("invalid_audio", "Не удалось прочитать аудиофайл.") from exc


def extract_image(path: Path, settings) -> str:
    import pytesseract

    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            return pytesseract.image_to_string(
                image, lang=settings.ocr_language, timeout=min(120, settings.job_timeout_seconds)
            )
    except pytesseract.TesseractNotFoundError as exc:
        raise ProcessingError(
            "ocr_unavailable", "Tesseract не установлен в окружении worker."
        ) from exc
    except pytesseract.TesseractError as exc:
        raise ProcessingError(
            "ocr_failed", "Ошибка OCR. Проверьте установленные языковые модели Tesseract."
        ) from exc
    except RuntimeError as exc:
        raise ProcessingError("ocr_timeout", "Превышено время распознавания изображения.") from exc


def extract_audio(path: Path, settings) -> tuple[str, dict]:
    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(
            settings.whisper_model,
            device="cpu",
            compute_type="int8",
            cpu_threads=2,
            download_root=str(settings.model_dir),
            local_files_only=True,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise ProcessingError(
            "audio_model_unavailable",
            "Сначала загрузите модель командой python -m app.prepare_models.",
        ) from exc
    segments, info = model.transcribe(str(path), beam_size=3, vad_filter=True)
    return "\n".join(segment.text.strip() for segment in segments), {"language": info.language}
