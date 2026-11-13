# Разработка и запуск

## Окружение

Нужны Docker с Compose v2 (`--wait`), Python 3.11 и uv в PATH.
Локальные команды проверялись с uv 0.9.9 и 0.13.8; установка конкретной
версии локально не обязательна. Docker и CI используют uv 0.9.9.
Все Python-окружения устанавливаются из `backend/uv.lock` с `--locked`.

```sh
make setup
make lint
make test
```

`make lint` проверяет lint и форматирование всего backend, включая прежний код.
`make test` собирает тесты из `backend/tests`, кроме пяти прежних SQLite/ORM
модулей, явно перечисленных в `PROTOTYPE_TESTS` в Makefile. Новые тестовые
модули попадают в запуск автоматически. Проверка прежнего приложения
с его зависимостями описана ниже и выполняется отдельным CI job.
Результаты проверок и ограничения реализации находятся в [плане](work-plan.md).

## PostgreSQL и API

```sh
cp .env.example .env
# Замените пароль в обеих переменных POSTGRES_PASSWORD и NOTE_AGENT_DATABASE_URL.
make up
make logs
make down
```

Compose читает параметры из `.env`, публикует API только на loopback
и не публикует порт БД. Специальные символы пароля в URL должны быть
percent-encoded. Данные БД хранятся в `<compose-project>_postgres-data`;
`make down` не удаляет volume. Для отдельного проекта задайте, например,
`make up COMPOSE='docker compose -p notes-dev'`. Не используйте имена проектов
и volumes экспериментов или чужих проверок.

Docker запускает `app.api_app:app`. Нативный запуск API:

```sh
uv run --directory backend --locked uvicorn app.api_app:app --host 127.0.0.1 --port 8000
```

Нативные процессы читают экспортированные `NOTE_AGENT_*` переменные, а не `.env`.
`NOTE_AGENT_DATABASE_URL` задаёт URL PostgreSQL; `NOTE_AGENT_CORS_ORIGINS` —
разделённые запятыми адреса клиентов. Описание доступных маршрутов — `/docs`.

Проверка Docker-сборки, HTTP health, pgvector и сохранности SQL-данных:

```sh
COMPOSE_PROJECT_NAME=backend-check-unique NOTE_AGENT_PORT=18081 sh scripts/check-backend.sh
```

Скрипт использует только новый volume: отказывается работать, если volume уже
существует, и сохраняет его после остановки сервисов. Для повторного запуска
выберите новое имя проекта. Модели и Telegram token для проверки не нужны.

## Приложение-прототип и worker

Для прежнего приложения установите `prototype` extra из того же lockfile:

```sh
uv sync --project backend --locked --python 3.11 --extra prototype
```

API и worker запускаются в двух терминалах с одинаковым абсолютным
`NOTE_AGENT_DATA_DIR`. Ниже используется отдельный каталог тестовых данных;
для существующих данных укажите их каталог. Порт 8001 отделён от Compose API.

```sh
export NOTE_AGENT_DATA_DIR=/tmp/local-note-prototype-dev
uv run --directory backend --locked --extra prototype uvicorn app.main:app --host 127.0.0.1 --port 8001
```

```sh
export NOTE_AGENT_DATA_DIR=/tmp/local-note-prototype-dev
uv run --directory backend --locked --extra prototype python -m app.worker
```

Прототип использует SQLite и сам выполняет прежние Alembic-миграции при запуске.
`Settings` сохраняет прежние параметры: `NOTE_AGENT_DATA_DIR`,
`NOTE_AGENT_WHISPER_MODEL` (по умолчанию `base`), `NOTE_AGENT_OCR_LANGUAGE`
(`rus+eng`), `NOTE_AGENT_KNOWLEDGE_ADAPTER` (путь `package.module:factory`),
лимиты `NOTE_AGENT_IMAGE_MAX_BYTES`, `NOTE_AGENT_AUDIO_MAX_BYTES`,
`NOTE_AGENT_AUDIO_MAX_SECONDS`, `NOTE_AGENT_IMAGE_MAX_PIXELS`,
`NOTE_AGENT_TEXT_MAX_CHARS`, `NOTE_AGENT_HTML_MAX_BYTES`,
`NOTE_AGENT_WEB_TIMEOUT_SECONDS`, `NOTE_AGENT_JOB_TIMEOUT_SECONDS` и CORS origins.
API и worker должны использовать одинаковые значения.

Для реального OCR нужен системный Tesseract с языками rus+eng.
Для реального ASR отдельно подготовьте веса в каталоге модели прототипа:

```sh
uv run --directory backend --locked --extra prototype python -m app.prepare_models
```

Команда подготовки загружает веса по сети; обычный запуск API их не скачивает.
Не используйте каталоги экспериментальных моделей для этих команд.

Сбор и запуск всех backend-тестов, включая прежние тесты прототипа:

```sh
uv run --directory backend --locked --extra prototype pytest tests --collect-only
uv run --directory backend --locked --extra prototype pytest tests
```

Модельные сценарии прежних тестов используют тестовые адаптеры/подмены;
проверка реальных OCR/ASR не подменяется этим прогоном.
`backend/requirements.lock` — исторический файл: установка, Docker и CI
используют `uv.lock`. После работы с прототипом `make setup` возвращает
минимальное окружение без extras.

## Модельные клиенты

`app.model_clients.contracts` определяет `TextGenerator`, `Embedder`, `Transcriber`.
Реализации: `OpenAITextGenerator`, `OllamaEmbedder`, `HTTPTranscriber`.
Они получают отдельные settings через конструктор; `Settings.from_env()` читает
только экспортированное окружение своего префикса. `.env` автоматически не читается.
Ни один клиент не обращается к БД и не выполняет инструменты приложения.
`testing.py` содержит автономные `FakeTextGenerator`, `FakeEmbedder`,
`FakeTranscriber` с явными фикстурами для тестов последующих задач.
Общие Python-зависимости не расширены: Pydantic уже входит в backend,
HTTP-клиент и нативный ASR-сервис используют стандартную библиотеку.

Настройки клиентов:

| Префикс | Переменные (значения по умолчанию) |
|---|---|
| `LLM_` | `BASE_URL=http://127.0.0.1:11435/v1`, `MODEL=qwen3:8b`, `API_KEY` (пусто), `TIMEOUT=180`, `MAX_TOKENS=512`, `OPTIONS={}` |
| `EMBEDDING_` | `BASE_URL=http://127.0.0.1:11435`, `MODEL=qwen3-embedding:0.6b`, `API_KEY` (пусто), `TIMEOUT=180`, `DIMENSIONS=1024`, `NUM_CTX=8192`, `NORM_TOLERANCE=0.01`, `QUERY_INSTRUCTION` из P02, `DIGEST` из manifest P02 |
| `ASR_` | `BASE_URL=http://127.0.0.1:18082`, `API_KEY` (пусто), `TIMEOUT=600`, `MAX_BYTES=52428800` |

Timeout задаётся в секундах для HTTP socket operations, не как общий deadline
всего задания. Ограничения заданий остаются ответственностью worker.
`LLM_OPTIONS` — JSON-объект параметров профиля; нельзя подменять им messages,
model, tools, response_format, stream и max_tokens. Локальный проверенный Qwen
использует `{"temperature":0,"seed":42,"reasoning_effort":"none"}`;
по умолчанию reasoning option отсутствует. Для другого провайдера задайте его
собственные URL, model, key и options; наличие tools/JSON mode требует отдельного
smoke. Автоматического cloud fallback, повторов и перенаправлений HTTP нет.
Streaming и строгая JSON Schema не реализованы.

`chat(messages, tools)` возвращает `Generation` с текстом и проверенными
`ToolCall`. Для определения инструмента передайте `Tool(name, description,
ArgumentsModel)`, где Pydantic-модель аргументов задаёт строгие типы, ограничения
и `extra="forbid"`. Неизвестные имена, повторные ID, неверный JSON/аргументы,
пустой или оборванный ответ дают `ModelClientError`. Клиент не исполняет tool call:
последующий вызывающий код добавляет `generation.as_message()` и `Message` с
`role="tool"`, `tool_call_id`, строкой результата. `json(messages, ResultModel)`
выполняет отдельный запрос с `response_format={"type":"json_object"}` и проверяет
результат через Pydantic. Поля и содержательные ограничения задаёт вызывающая
модель результата; JSON mode не гарантирует правильность фактов.

`documents(texts)` не добавляет инструкцию; `queries(texts)` добавляет P02
`Instruct: ...\nQuery: ...`. Используется NFC и нормализация пробелов без усечения.
Перед каждым непустым пакетом `/api/tags` проверяет имя и digest модели.
`/api/embed` получает `truncate=false`, фиксированный контекст; ответ обязан
содержать ту же модель, нужное число векторов размерности 1024 и конечные числа.
Норма должна отличаться от 1 не более чем на 0.01, затем корректируется до 1.
`identity` включает модель и digest. При смене модели индекс должен перестраиваться
в последующей задаче. Поддержка других embedding API не заявляется.

Ошибки клиентов имеют безопасный `code`: `timeout`, `connection_failed`,
`http_protocol_error` (в том числе неполный HTTP-ответ),
`http_<status>`, `invalid_json`, `invalid_generation`,
`invalid_structured_result`, `embedding_identity_mismatch`, `invalid_embeddings`,
`invalid_transcript`. Тела ошибок провайдера, ключи и входные тексты не логируются.

### Нативный ASR на Mac

Приложение посылает тело файла POST `/transcribe` (`application/octet-stream`)
и получает ровно `{"text":"...","language":"ru"}`. Расширение файла и MIME
не определяют формат: ffprobe и полное декодирование FFmpeg проверяют OGG/Opus,
PCM WAV или MP3. Пустое/повреждённое аудио и превышение лимитов отклоняются.
Результат не содержит сегментов/таймкодов. GET `/health` проверяет доступность
HTTP-процесса, не качество модели.

Переиспользуйте существующие runtime и веса P03 **по абсолютным путям**;
скрипт ничего не скачивает и не копирует. FFmpeg/ffprobe должны быть в PATH.
`ASR_RUNTIME`, `ASR_MODEL` обязательны для сервиса. Необязательны:
`ASR_BIND=127.0.0.1`, `ASR_PORT=18082`, `ASR_RUNTIME_TIMEOUT=600`,
`ASR_MAX_SECONDS=1800`, `ASR_MAX_BYTES=52428800`.
Сервис последовательный (`HTTPServer`), без новой очереди; временные вход и PCM
удаляются после запроса. P03-параметры whisper.cpp сохранены: auto language,
6 threads, beam/best-of 5, temperature 0, temperature increment 0; `-nt` отсутствует,
внутреннее выравнивание включено, временные префиксы удаляются из stdout в памяти.
Парсер языка рассчитан на проверенный whisper.cpp v1.9.5.

```sh
# Из корня worktree; MODEL_ROOT — существующий checkout с моделями P01–P03.
export MODEL_ROOT=/absolute/path/to/existing-checkout
export ASR_RUNTIME="$MODEL_ROOT/experiments/p03/local/build/bin/whisper-cli"
export ASR_MODEL="$MODEL_ROOT/experiments/p03/local/ggml-large-v3-turbo-q5_0.bin"
PYTHON="$PWD/backend/.venv/bin/python" sh scripts/models/serve-asr.sh
# Остановка собственного сервиса — Ctrl-C.
```

В контейнере `ASR_BASE_URL=http://host.docker.internal:18082`, а не localhost;
аналогично `LLM_BASE_URL=http://host.docker.internal:11435/v1` и
`EMBEDDING_BASE_URL=http://host.docker.internal:11435`. Нативный бинарник macOS
запускается сервисом хоста, не контейнером. Контейнер → модельные сервисы
ещё не проверен. Доступность host.docker.internal
зависит от настроек Docker/хоста: проверьте её в своей среде перед интеграцией.
Изменение bind допускается явно в закрытой сети хоста; сервис не предоставляет
авторизацию/TLS и не предназначен для публикации в Интернет.

### Тесты и последовательный smoke

```sh
uv run --directory backend --locked pytest tests/model_clients -q
uv run --directory backend --locked ruff check app/model_clients tests/model_clients
uv run --directory backend --locked ruff format --check app/model_clients tests/model_clients
sh -n scripts/models/serve-asr.sh
```

Для реальных проверок используйте только публичные фикстуры smoke. Результаты
содержат тексты/векторы и не предназначены для пользовательских материалов.
Каждый `--output` должен быть новым: файл создаётся эксклюзивно; предыдущие
результаты не перезаписываются. Родительский каталог создайте заранее.
Из корня worktree при запущенном собственном Ollama (P01, общий serve.sh):

```sh
export LLM_BASE_URL=http://127.0.0.1:11435/v1
export LLM_OPTIONS='{"temperature":0,"seed":42,"reasoning_effort":"none"}'
uv run --directory backend --locked python -m app.model_clients.smoke generation --output /tmp/t07-generation-unique.json
export EMBEDDING_BASE_URL=http://127.0.0.1:11435
uv run --directory backend --locked python -m app.model_clients.smoke embeddings --output /tmp/t07-embeddings-unique.json
```

Ollama для другого worktree запускайте с явными `OLLAMA_RUNTIME_DIR`,
`OLLAMA_MODELS` из существующего checkout, отдельным `OLLAMA_LOG_DIR` и свободным
`OLLAMA_HOST`. Это исключает копирование весов и конфликт портов.
Сначала завершите свой Ollama, затем запустите нативный ASR в другом терминале:

```sh
export ASR_BASE_URL=http://127.0.0.1:18082
uv run --directory backend --locked python -m app.model_clients.smoke asr \
  --audio "$MODEL_ROOT/experiments/p03/local/audio/voice.ogg" \
  --audio "$MODEL_ROOT/experiments/p03/local/audio/voice.wav" \
  --audio "$MODEL_ROOT/experiments/p03/local/audio/voice.mp3" \
  --output /tmp/t07-asr-unique.json
```

Файлы `voice.*` должны происходить от публичного `public-original.mp3` P03,
не от личной записи. Их подготовка описана в отчёте P03; существующие файлы
не требуют повторной загрузки. После smoke остановите свой ASR Ctrl-C.
Статус `pass` означает прохождение клиентских контрактов; качество содержания
проверяется отдельно. Remote/cloud и реальный Telegram требуют отдельных проверок.
