from faster_whisper.utils import download_model

from app.config import Settings


def main():
    settings = Settings.from_env()
    settings.model_dir.mkdir(parents=True, exist_ok=True)
    download_model(settings.whisper_model, cache_dir=str(settings.model_dir))
    print("Audio model is ready.")


if __name__ == "__main__":
    main()
