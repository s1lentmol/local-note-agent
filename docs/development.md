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
модулей, явно перечисленных в `PROTOTYPE_TESTS` в Makefile, и PostgreSQL-тестов
(их запускает `make test-db`). Новые тестовые
модули попадают в запуск автоматически. Проверка прежнего приложения
с его зависимостями описана ниже и выполняется отдельным CI job.
Результаты проверок и ограничения реализации находятся в [плане](work-plan.md).

## PostgreSQL и API

```sh
cp .env.example .env
# Замените пароль в обеих переменных POSTGRES_PASSWORD и NOTE_AGENT_DATABASE_URL.
docker compose build api
docker compose up --wait db
make migrate
make up
make logs
make down
```

Compose читает параметры из `.env`, публикует API только на loopback
и не публикует порт БД. Специальные символы пароля в URL должны быть
percent-encoded. Данные БД хранятся в `<compose-project>_postgres-data`;
`make down` не удаляет volume. Для отдельного проекта задайте, например,
`export COMPOSE_PROJECT_NAME=notes-dev` перед всеми командами подготовки и запуска.
Не используйте имена проектов и volumes экспериментов или чужих проверок.
Первый запуск требует порядка: сборка образа → только БД с ожиданием готовности →
явные миграции → API. `make up` запускает уже подготовленную схему и сам миграции
не выполняет. Повторный `make migrate` безопасен.

Docker запускает `app.api_app:app`. Для нативного запуска нужна отдельно
доступная PostgreSQL/pgvector: Compose не публикует её порт для хоста.
После установки окружения экспортируйте URL этой БД и сначала примените миграции:

```sh
export NOTE_AGENT_DATABASE_URL=postgresql://user:password@127.0.0.1:5432/note_agent
uv run --directory backend --locked python migrate.py
uv run --directory backend --locked uvicorn app.api_app:app --host 127.0.0.1 --port 8000
```

Нативные процессы читают экспортированные `NOTE_AGENT_*` переменные, а не `.env`.
`NOTE_AGENT_DATABASE_URL` задаёт URL PostgreSQL; `NOTE_AGENT_CORS_ORIGINS` —
разделённые запятыми адреса клиентов. Описание доступных маршрутов — `/docs`.

Проверка первого запуска на пустом volume: Docker-сборка, явные миграции
(включая повтор), HTTP health, pgvector, создание/чтение заметки и сохранность
SQL-данных, полной заметки и побайтового оригинала после остановки и повторного запуска:

```sh
COMPOSE_PROJECT_NAME=backend-check-unique NOTE_AGENT_PORT=18081 sh scripts/check-backend.sh
```

Скрипт использует новые volumes PostgreSQL и оригиналов: отказывается работать,
если хотя бы один уже существует, и сохраняет их после остановки сервисов. Для повторного запуска
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

Сбор и запуск backend-тестов без PostgreSQL, включая прежние тесты прототипа:

```sh
uv run --directory backend --locked --extra prototype pytest tests --ignore=tests/postgres --collect-only
uv run --directory backend --locked --extra prototype pytest tests --ignore=tests/postgres
```

Модельные сценарии прежних тестов используют тестовые адаптеры/подмены;
проверка реальных OCR/ASR не подменяется этим прогоном.
`backend/requirements.lock` — исторический файл: установка, Docker и CI
используют `uv.lock`. После работы с прототипом `make setup` возвращает
минимальное окружение без extras.

## SQL-схема, пул и DTO

Целевые модули — `app.postgres` и `app.dto`; старые `app.db` и `app.schemas`
обслуживают прототип. Импорт модулей не открывает соединения. API не выполняет
миграции. Перед первым запуском прикладных процессов:

```sh
docker compose build api
docker compose up --wait db
make migrate
make up
```

Повторный `make migrate` безопасен. Для нативного запуска экспортируйте
`NOTE_AGENT_DATABASE_URL` с доступным адресом PostgreSQL и выполните:

```sh
uv run --directory backend --locked python migrate.py
```

Мигратор использует session advisory lock, SHA-256 исходных байтов файлов и
отдельную транзакцию на каждую миграцию. Имена: `001_initial.sql`, `002_name.sql`
без пропусков и повторов. Применённые файлы не редактируются и не удаляются:
изменение checksum останавливает запуск. При ошибке откатывается вся текущая
миграция, предыдущие остаются применёнными. SQL-файлы должны содержать только
транзакционный DDL, без BEGIN/COMMIT и нетранзакционных команд.

Сервисы явно управляют жизненным циклом `Database(url)` (`open`/`close` либо
контекстный менеджер). `database.transaction()` выдаёт соединение и атомарную
транзакцию, освобождает его при успехе и исключении. `database.connection()`
выдаёт autocommit-соединение для отдельных запросов. Используйте параметры SQL,
не подставляйте значения строковой интерполяцией. Пул регистрирует pgvector
при подключении; схема должна быть применена до открытия пула. Модельные вызовы
и извлечение файлов выполняются вне транзакций.

```sh
make test-db
```

Команда создаёт PostgreSQL 17 / pgvector 0.8.1 в отдельном контейнере со случайным
loopback-портом и без volumes, ждёт readiness и удаляет контейнер после тестов.
Она не читает `NOTE_AGENT_DATABASE_URL` или `.env`. Тесты создают отдельную схему
на сценарий; отсутствие специальной тестовой БД — ошибка, а не skip.
Для ручного запуска допустима только выделенная БД с именем `t02_tests`:

```sh
NOTE_AGENT_TEST_DATABASE_URL=postgresql://user:password@127.0.0.1:5432/t02_tests \
  uv run --directory backend --locked pytest tests/postgres
```

`make test` проверяет целевой backend без PostgreSQL и prototype extra;
`make test-db` обязателен для схемы,
мигратора и пула. CI выполняет обе команды. Полный локальный набор:

```sh
make lint
make test
make test-db
uv run --directory backend --locked --extra prototype pytest tests --ignore=tests/postgres
```

DTO описывают контракты; текстовый CRUD подключён в T03, остальные маршруты
добавляются в соответствующих задачах. Идентичность
embedding-профиля хранится в БД; адаптер и обработчик должны проверять её,
не усекать векторы и не заменять модель молча. Проверки lease при публикации
будут реализованы worker/processing задачами.

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

## CRUD текстовых заметок (T03)

Целевое приложение `app.api_app:app` предоставляет только health и CRUD:
`POST /api/v1/notes`, `GET /api/v1/notes`, `GET/PATCH/DELETE /api/v1/notes/{id}`.
Прежний `app.main` и его маршруты остаются отдельным прототипом.
При заданном `NOTE_AGENT_DATABASE_URL` lifespan открывает пул PostgreSQL и
закрывает его при остановке. Миграции обязательны **до** запуска API;
приложение их не выполняет. При пустом URL доступен health, CRUD отвечает `503`.
Если заданный URL недоступен, запуск завершается ошибкой. Отсутствующие миграции
не применяются автоматически; подготовку схемы проверяйте отдельной командой.
Модельные endpoint и ключи CRUD не требуются.

```sh
export NOTE_AGENT_DATABASE_URL=postgresql://user:password@127.0.0.1:5432/note_agent
uv run --directory backend --locked python migrate.py
uv run --directory backend --locked uvicorn app.api_app:app --host 127.0.0.1 --port 8000
```

Пример создания (в другом терминале):

```sh
curl -i http://127.0.0.1:8000/api/v1/notes \
  -H 'Content-Type: application/json' \
  -d '{"source_type":"text","text":"Первая строка\nПолный исходный текст"}'
curl 'http://127.0.0.1:8000/api/v1/notes?limit=20&offset=0&source_type=text&ingestion_status=ready'
```

Заголовок необязателен: первая строка текста, не более 200 символов.
Текст сохраняется с пробелами и переводами строк; пустое содержимое, NUL,
более 100000 символов и посторонние поля отклоняются. Новая заметка сразу
получает `ingestion_status=ready`. При `NOTE_AGENT_PROCESSING_ENABLED=true`
в той же транзакции создаётся один `process`, статус обработки — `queued`.
При выключенном флаге статус — `not_requested`, задание не создаётся.
Фильтры списка: `source_type`, `ingestion_status`, `processing_status`;
порядок всегда `created_at DESC, id DESC`. `total` и страница читаются в одном
снимке PostgreSQL. Лимит 1–100, offset неотрицательный.

PATCH принимает `title` и/или `extracted_text`, без null/пустых значений;
неготовое извлечение даёт `409`, неизвестная/удалённая заметка — `404`.
Исходный `source.original_text` не меняется. В одной транзакции блокируется
заметка, затем отменяются её активные jobs, очищаются chunks, автоматические
связи, enrichment и автоматические поля/ошибки. Ручные теги и подборки сохраняются.
При включённом `NOTE_AGENT_PROCESSING_ENABLED` PATCH ставит один новый
`process` в той же транзакции. При выключенном флаге статус становится
`not_requested` и новое задание не создаётся.

DELETE блокирует заметку перед jobs, ставит `deleted_at`, отменяет активные
задания и ставит один `cleanup`. Ответ `204` означает, что заметка уже скрыта
из CRUD и поиска. Подключённый PostgreSQL worker асинхронно удаляет оригиналы
и производные данные. Повторный DELETE после ошибки возвращает тот же
`cleanup` в очередь. Подробности — в разделе [T14](#t14-правка-удаление-и-очистка).
Маршруты будущих функций не зарегистрированы. Ошибки имеют вид
`{"error":{"code":"validation_error","message":"Некорректные параметры запроса."}}`;
валидация — `422`, сбой БД во время запроса — `503`, неожиданная ошибка — `500`.

Общий Python-слой для следующих адаптеров не зависит от FastAPI:

```python
from app.dto import NoteListQuery, NotePatch, TextNoteCreate
from app.postgres import Database
from app.services.notes import NotesService

with Database(database_url) as database:
    notes = NotesService(database)
    note = notes.create(TextNoteCreate(source_type="text", text="Исходный текст"))
    full_note = notes.get(note.id)
    page = notes.list(NoteListQuery(limit=20))
    edited = notes.patch(note.id, NotePatch(extracted_text="Исправленный текст"))
    notes.delete(note.id)
```

`NotesService` принимает целевые DTO и UUID; самостоятельно владеет короткими
транзакциями. `NoteError` содержит безопасные `code/message`, HTTP-коды назначает
API. Все SQL-запросы находятся в repositories; ввод передаётся параметрами.
Для CRUD и SQL обязательна `make test-db`: она использует отдельный контейнер
PostgreSQL/pgvector без volumes. Тесты включают исходный текст, rollback при
сбое посреди записи, отмену jobs, фильтры/пагинацию и порядок блокировок.
`make test`/`make lint` проверяют остальной целевой backend; старые тесты
запускаются с `--extra prototype` по инструкции выше. Новых зависимостей,
миграций и отдельных CI-команд для T03 не требуется.

## T06: последовательный PostgreSQL worker

Отдельная точка входа — `app.postgres_worker`; прототип `app.worker` остаётся
прежним. Схему нужно применить заранее командой мигратора из раздела SQL выше.
Запускайте **один** экземпляр. Worker не читает `.env` и не запускает миграции.

```sh
# Экспортируйте NOTE_AGENT_DATABASE_URL для целевой PostgreSQL с применённой схемой.
# Подключите factory своего прикладного обработчика (когда он реализован):
export NOTE_AGENT_PG_WORKER_HANDLERS=your_package.handlers:build_handlers
uv run --directory backend --locked python -m app.postgres_worker
# SIGINT / SIGTERM завершают текущий дочерний процесс и закрывают пул.
```

Изначально T06 поставлял worker без прикладных обработчиков. Без factory worker
ставит занятому job `failed` с `handler_missing`; фиктивного успеха нет.
`--retry UUID` явно переводит только подходящий `failed` job в `queued` и выходит:

```sh
uv run --directory backend --locked python -m app.postgres_worker --retry JOB_UUID
```

Это CLI и метод `JobsRepository.retry`, HTTP-маршрут остаётся в области T03.
Retry сохраняет attempts; новая попытка увеличивает attempts и получает новый
UUID token. При активном job того же вида, удалённой заметке для extract/process
или другом статусе retry возвращает отказ (CLI exit 1). Cleanup допускается
для служебной записи с `deleted_at`, пока нет успешно завершённого cleanup.

Настройки отдельного worker, в секундах:

| Переменная | По умолчанию |
|---|---:|
| `NOTE_AGENT_PG_WORKER_LEASE_SECONDS` | 30 |
| `NOTE_AGENT_PG_WORKER_HEARTBEAT_SECONDS` | 5 |
| `NOTE_AGENT_PG_WORKER_TIMEOUT_SECONDS` | 600 |
| `NOTE_AGENT_PG_WORKER_POLL_SECONDS` | 1 |
| `NOTE_AGENT_PG_WORKER_STOP_GRACE_SECONDS` | 1 |

Все интервалы конечные и положительные; heartbeat меньше lease. Timeout считает
время от запуска дочернего процесса, включая его старт. При timeout job получает
`job_timeout`; при исключении/аварийном выходе — `handler_failed`; при остановке
активного worker — `worker_stopped`. Текст исключения не сохраняется в БД.
Ручной retry нужен и после прерванного остановкой задания. После аварии процесса
оставшийся `processing` восстанавливается в `queued` по истечении lease.
Проверка восстановления идёт перед каждым занятием. Failed автоматически
не повторяется; backoff и отдельных очередей нет.

### Интерфейс обработчиков и публикация для T03/T08/T11

Factory без аргументов возвращает словарь
`{"process": HandlerBinding(compute, writer), ...}` из
`app.job_runner.worker`. Поддерживаются только extract/process/cleanup.
`compute(job, context)` — функция на уровне импортируемого модуля либо другой
spawn-picklable callable. Она выполняется в отдельном процессе без пула/соединений
родителя, получает `Job(id, note_id, kind, lease_token, attempts)` и вызывает
`context.publish(payload)` для каждой готовой стадии. После успешного return
родитель отдельно завершает job с той же проверкой lease/token. Исключение
останавливает оставшиеся стадии, ранее опубликованные данные сохраняются.
Модельные и файловые ресурсы обработчик создаёт в дочернем процессе; закрывает
их при обычном выходе. При принудительном kill исполнение finally не гарантировано.
Не передавайте открытые ресурсы через factory и не создавайте собственные workers.

`writer(conn, job, payload)` выполняется в родителе через единственную границу
`JobsRepository.publish`. Это **только короткие параметризованные SQL-записи**,
без моделей, чтения файлов и сетевых вызовов. Нельзя делать commit/rollback,
менять строку job или физически удалять notes; cleanup оставляет tombstone.
Отсутствие writer при попытке publish означает ошибку, без успешного завершения.
Прямой вызов `repository.publish(job, writer, payload, complete=True)` позволяет
атомарно записать финальные данные и завершить job в прикладном SQL-коде.
Для одношагового extract production `HandlerBinding` задаёт
`complete_on_publish=True`: worker вызывает тот же `publish(..., complete=True)`.
Текст, ready, постановка process и завершение extract фиксируются вместе.
После финальной публикации отдельный `done` не завершает job повторно.
Process сохраняет промежуточные публикации индекса и обогащения.

Порядок каждой защищённой операции: `notes FOR UPDATE`, затем `jobs FOR UPDATE`,
проверка принадлежности/вида, processing/token и `lease_until > clock_timestamp()`.
Для extract/process заметка не должна быть удалена; cleanup обрабатывает только
удалённую служебную запись. Lease проверяется после ожидания блокировок и ещё раз
после SQL callback: если она истекла во время записи, транзакция откатывается.
Heartbeat имеет те же ограничения и не оживляет истёкшее/отменённое занятие.
Занятие использует `FOR UPDATE SKIP LOCKED` для заметки и queued job, сохраняя
тот же порядок блокировок. Сервисы T03 должны блокировать заметку перед jobs
при отмене, повторе и создании активного job; сама схема этот протокол не навязывает.
Общие DTO, notes service/repository и схема в T06 не менялись.

### Проверки T06 на PostgreSQL/pgvector

Из корня worktree:

```sh
# Весь SQL-набор T02 + jobs/worker T06:
sh scripts/test-postgres.sh -q
# Только T06, с той же одноразовой БД:
sh scripts/test-postgres.sh -q -k jobs_worker
uv run --directory backend --locked ruff check app/jobs_repository.py app/job_runner app/postgres_worker.py tests/postgres/test_jobs_worker.py
uv run --directory backend --locked ruff format --check app/jobs_repository.py app/job_runner app/postgres_worker.py tests/postgres/test_jobs_worker.py
```

Скрипт создаёт отдельный контейнер PostgreSQL 17 / pgvector 0.8.1 без volumes,
со случайным loopback-портом и отдельной схемой на сценарий; удаляет контейнер
после проверки. Имя тестовой БД `t02_tests` — контракт переиспользуемой фикстуры,
а не доступ к существующей БД T02/экспериментов. Тесты используют синтетические
данные, настоящие блокировки/SQL, lease 0.15–2 s и тестовые обработчики без моделей.
Проверяются токены/отмена/истечение, SKIP LOCKED, heartbeat, ручной retry,
последовательность, timeout/исключение/аварийный выход, промежуточная публикация,
SIGTERM в idle и при вычислении, принудительное завершение ребёнка и закрытие пула.
Интеграция прикладных статусов заметок, настоящих моделей и удаления файлов
остаётся в следующих назначенных задачах; готовность E2 этим не подтверждается.

## T08: индексирование через process

Создание текста в общем `NotesService` ставит process в той же транзакции,
если `NOTE_AGENT_PROCESSING_ENABLED=true`. По умолчанию флаг выключен:
CRUD сохраняет `processing_status=not_requested`, не обращается к моделям
и не создаёт process. Compose передаёт флаг API из `.env`.
Флаг управляет автоматической постановкой process при создании и правке; уже поставленные jobs,
явный retry и команда обработки существующих заметок выполняются независимо от него.

Целевой PostgreSQL worker запускается отдельным нативным процессом. Сначала
подготовьте доступную с хоста PostgreSQL/pgvector и примените миграции; URL
Compose `@db:5432` с хоста недоступен (порт БД по умолчанию не опубликован).
Порядок первого запуска остаётся: готовность БД → явные миграции → API/worker.
Worker не включён в Compose; контейнерный доступ к модельному сервису в T08
не проверяется и не подразумевается нативным smoke.

```sh
# Для всех нативных процессов используйте один URL подготовленной БД.
export NOTE_AGENT_DATABASE_URL=postgresql://user:password@127.0.0.1:5432/note_agent
uv run --directory backend --locked python migrate.py
# Ollama запускается отдельно с уже подготовленными весами P02 (см. раздел моделей).
export EMBEDDING_BASE_URL=http://127.0.0.1:11435
export NOTE_AGENT_PROCESSING_ENABLED=true
uv run --directory backend --locked python -m app.postgres_worker
# В другом терминале, с теми же экспортированными настройками:
uv run --directory backend --locked uvicorn app.api_app:app --host 127.0.0.1 --port 8000
# Явная постановка существующих ingestion_status=ready заметок:
uv run --directory backend --locked python -m app.services.processing
```

Production factory по умолчанию — `app.indexing:handlers`, регистрирует
extract, process и cleanup через `HandlerBinding`. `NOTE_AGENT_PG_WORKER_HANDLERS` задаёт
явный override для тестов/интеграции; пустое значение отключает все обработчики.
Extract использует строгий TXT/Markdown extractor T11; инструкции ниже.

`NOTE_AGENT_CHUNK_SIZE=600`, `NOTE_AGENT_CHUNK_OVERLAP=80` задают приближённые
токены: группа до четырёх букв/цифр либо один знак пунктуации. Это оценка,
а не tokenizer модели. Исходный текст внутри границ сохраняется; короткий текст
даёт один chunk. `NOTE_AGENT_CHUNK_BATCH_SIZE=16` ограничивает пакет embeddings.
Вход документа модели — заголовок, пустая строка, текст chunk; query instruction
к документам не добавляется. Верхний контекст и `truncate=false` контролирует
адаптер T07; превышение контекста даёт failed, текст заметки сохраняется.
Все `EMBEDDING_*` настройки остаются настройками T07.

Профиль пространства из настроек (модель, digest, 1024, cosine, l2, полный
query prefix) сверяется с `embedding_profiles` перед вычислением и повторно
под `FOR UPDATE` при публикации. Адаптер проверяет реальный digest endpoint;
обработчик проверяет identity, размерность, количество, конечность и L2 векторов.
Вычисления выполняются в дочернем процессе после закрытия соединения чтения.
Индекс заменяется атомарно только через `JobsRepository.publish` с блокировками
заметка → job → профиль. Истёкшая lease откатывает и замену chunks, и завершение.
Промежуточный индекс сохраняется при последующей ошибке; T13 продолжит ту же
функцию после публикации, без второй очереди и index/enrich jobs.

T06 синхронизирует статусы process с заметкой: claim → processing,
complete → ready, fail → failed с безопасной ошибкой, retry/recovery → queued.
Ошибки модели/профиля отображаются как безопасный `handler_failed` без исходного
exception, текста заметки или ответа провайдера. Summary/теги ещё не вычисляются;
ready сейчас означает завершение подключённой части process (индекса).

Команда backlog блокирует каждую заметку перед jobs, повторно проверяет ready
и отсутствие удаления, пропускает активный process и failed заметки.
Повтор команды во время обработки не создаёт дубликатов. После завершения
она может явно поставить новое полное переиндексирование. Отдельный failed job
повторяется через существующий механизм T06, с тем же ID и новым token при claim:

```sh
curl --fail -X POST http://127.0.0.1:8000/api/v1/notes/NOTE_UUID/retry \
  -H 'Content-Type: application/json' -d '{"stage":"process"}'
# Для администратора остаётся CLI T06:
uv run --directory backend --locked python -m app.postgres_worker --retry JOB_UUID
```

API возвращает `202 Accepted` и сохранённую полную карточку с queued;
недоступный повтор — 409, удалённая
заметка — 404, неверное тело — 422. `stage=extract` повторяет failed извлечение.
HTTP retry использует `JobsRepository.retry` для последнего job выбранной стадии внутри
транзакции с блокировкой заметки. Правки и cleanup описаны в разделе T14.

Постоянные проверки (входят в существующие Makefile/CI команды):

```sh
make lint test
sh scripts/test-postgres.sh -q
# Только T08 SQL-сценарии:
sh scripts/test-postgres.sh -q -k process_index
```

Тестовые embeddings детерминированы, настоящая SQL-БД создаётся скриптом без
volumes. CLI-сценарий отдельно использует настоящий T07 HTTP-клиент против
локального тестового endpoint. Это не smoke реальной модели. Реальный smoke:
`uv run --directory backend --locked python -m app.model_clients.smoke embeddings
--output /private/tmp/t08-embeddings.json` с доступным `EMBEDDING_BASE_URL`.

## T04: веб-интерфейс заметок

Отдельный `frontend/` использует React/TypeScript/Vite и настоящий CRUD API
`app.api_app:app`. Нужны Node.js 24+ и npm; проверено с Node 26.13.0 / npm 11.20.0.
Версии frontend-зависимостей зафиксированы в `frontend/package-lock.json`.
API и миграции запускаются по разделу «CRUD текстовых заметок (T03)» выше.
Модели для CRUD не нужны. Не подключайте ручные проверки к пользовательской БД.

Из корня своего worktree:

```sh
npm --prefix frontend ci
# API уже запущен на 127.0.0.1:8000:
npm --prefix frontend run dev -- --port 5174 --strictPort
# Для другого адреса API задайте его только процессу Vite:
NOTE_AGENT_API_TARGET=http://127.0.0.1:18084 npm --prefix frontend run dev -- --port 5174 --strictPort
```

Откройте `http://127.0.0.1:5174`. Vite пересылает `/api` к API;
по умолчанию `http://127.0.0.1:8000`. Это серверный параметр proxy, а не URL,
вшитый в браузерный bundle; CORS для такого запуска не требуется.
Не запускайте две команды dev одновременно на одном порту. Остановка — Ctrl-C.
Навигация использует hash-маршруты: карточку можно открыть по прямой ссылке.
Прототип и его способы запуска сохраняются.

Воспроизводимая проверка типов и production-сборки (без frontend-тестов,
согласно прямому указанию участника):

```sh
npm --prefix frontend run check
# Только TypeScript:
npm --prefix frontend run typecheck
# Локальный просмотр собранного dist, с тем же proxy /api:
npm --prefix frontend run preview -- --port 5174 --strictPort
```

Для ручной приёмки на тестовой БД: создать многострочный текст без заголовка,
открыть карточку, изменить заголовок и текущий текст, проверить неизменность
оригинала; отменить удаление, затем подтвердить его. Проверить пустой список,
все три фильтра и пагинацию (нужно больше одной страницы заметок), ошибки при
недоступном API и повтор загрузки. Повторить основные действия на узком экране.
После подключения T08 проверить queued/processing → ready/failed и ошибки
обработки; у неактивной карточки/скрытой вкладки фоновых запросов быть не должно.
При `NOTE_AGENT_PROCESSING_ENABLED=false` новый текст имеет `ready/not_requested`;
при включённой обработке T08 — `ready/queued` с дальнейшими состояниями worker.
Интерфейс отображает ответ API, не имитирует обработку и не предлагает кнопку retry.

Production-результат — `frontend/dist`. При интеграции нужен статический
сервер с proxy `/api` к `app.api_app`; Vite dev/preview предназначены для
локального запуска. Отдельный workflow `.github/workflows/frontend.yaml`
проверяет frontend на push/pull_request: Node.js 24, `npm ci` и `npm run check`
в каталоге `frontend`, с кэшем npm по `frontend/package-lock.json`. Браузерные
и тестовые зависимости не нужны. Backend workflow, Compose и Makefile не меняются.
Раздача dist и общий контейнерный запуск остаются отдельной интеграцией.
Результаты и ограничения проверки записаны в карточке T04.

## T09: самостоятельный гибридный поиск

После применения всех нумерованных миграций запускайте обычный
`app.api_app:app` (команды выше). Миграция `002_search.sql` добавляет хранимые
RU/EN tsvector и GIN-индексы; существующие заметки заполняются автоматически,
последующие изменения заголовка/текста сразу обновляют полнотекстовый индекс.
Dense использует chunks, опубликованные T08. Генеративный сервер не нужен.

```sh
curl --fail http://127.0.0.1:8000/api/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"симметричное шифрование","limit":20}'
```

Тело: `query` (непустой текст до 100000 символов без NUL), `limit` 1–100
(по умолчанию 20), необязательные `source_type`, `collection_id` (UUID),
`date_from`/`date_to` (ISO datetime с часовым поясом). Даты включают обе границы
и относятся к `notes.created_at`; `date_from` не позже `date_to`. Неизвестные
поля/неверные значения дают 422. Неизвестная подборка даёт пустую выдачу.
Ответ: `{"items":[{"note_id":"UUID","title":"...","preview":"...","score":0.03}]}`.
Preview — начало текущего текста, без разметки. Удалённые заметки исключены.

`SearchService.search(SearchRequest(...))` — общий синхронный Python-интерфейс
в `app.services.search`; HTTP использует тот же сервис. Сервису передаётся
общий `Database`; для детерминированных сценариев можно передать `Embedder`
и полный `SpaceProfile`. Диалоговый инструмент `search_notes` использует этот же сервис.

Переменные `NOTE_AGENT_SEARCH_*` читаются при создании сервиса:

| Суффикс | По умолчанию | Диапазон/назначение |
|---|---:|---|
| `DENSE_ENABLED` | `true` | `false` явно оставляет только FTS, без чтения embedding-настроек и endpoint |
| `LEXICAL_CANDIDATES` | 100 | 1–10000 заметок полнотекстового канала |
| `DENSE_CANDIDATES` | 100 | 1–10000 заметок dense-канала |
| `RRF_K` | 60 | 1–10000, константа сглаживания рангов |
| `PREVIEW_CHARS` | 300 | 1–2000 символов |

Оба канала применяют все фильтры до ограничения кандидатов. FTS ищет по
заголовку (вес A) и текущему тексту (B) через `plainto_tsquery` отдельно для
русского и английского; совпадение в любом словаре, ранг — больший из двух
`ts_rank_cd`. Слова запроса соединяются AND, стоп-слова обрабатывает PostgreSQL.
Dense — точное cosine расстояние до chunks, лучший chunk определяет ранг
заметки. Несколько chunks не увеличивают вклад заметки и не расходуют бюджет
других заметок. При тысячах заметок ANN и отдельный поисковый сервер не нужны.
CTE фильтров встраивается планировщиком, позволяя использовать GIN.

RRF складывает `1 / (RRF_K + rank)` каждого канала (rank начинается с 1).
Результат объединён по note ID, сортируется по score, затем UUID для стабильного
порядка при равенстве; `limit` применяется в конце. Если бюджеты кандидатов
меньше `limit`, выдача может быть короче. Score — показатель ранга, а не
вероятность ответа. Порог отсутствия ответа не применяется. Включённый dense
может вернуть ближайшие заметки и без буквального совпадения.

Используются настройки `EMBEDDING_*` T07 и его query instruction. Полный профиль
(модель, digest, размерность, metric, normalization, query instruction)
проверяется до запроса к модели и повторно под `FOR SHARE` до SQL retrieval;
блокировка удерживается до завершения транзакции, смена/удаление профиля ждёт.
Модельный запрос выполняется без занятого соединения/транзакции. Несовместимость
профиля даёт 503 `embedding_profile_mismatch`; ошибка endpoint/невалидный вектор —
503 `embedding_unavailable`. Автоматической смены модели или отката к FTS нет.
Пустой ответ — 200 с `items: []`. БД недоступна — 503 `database_unavailable`.

Постоянные проверки (новые файлы автоматически входят в Makefile и backend CI):

```sh
make lint test
sh scripts/test-postgres.sh -q
# Только поиск, включая EXPLAIN ANALYZE на 1000 синтетических заметок/chunks:
sh scripts/test-postgres.sh -q -s -k search
```

Отдельный реальный smoke не входит в обязательный детерминированный прогон.
При уже работающем endpoint с существующими весами P02:

```sh
EMBEDDING_BASE_URL=http://127.0.0.1:11435 NOTE_AGENT_SEARCH_SMOKE=1 \
  sh scripts/test-postgres.sh -q -s -k search_real_endpoint
```

Smoke создаёт одноразовую БД, индексирует только синтетический RU/EN корпус,
проверяет точные запросы и две переформулировки через настоящий T07 Embedder
и SearchService, печатает ранги/score; веса не скачивает и не копирует.
По умолчанию этот тест пропускается. Он требует доступного Docker и endpoint.

## T05: Telegram — текст и пересылки

Отдельный нативный процесс использует aiogram long polling и общий `NotesService`.
Подготовьте доступный с хоста PostgreSQL/pgvector, примените миграции и установите
backend из lockfile (см. раздел PostgreSQL выше). API не обязан работать для
приёма сообщений; для просмотра заметок запустите его и веб-интерфейс отдельно
с тем же URL БД.

Токен задаётся только серверному окружению процесса. Не записывайте его в Git,
аргументы команд или отчёты. В интерактивном zsh можно ввести без отображения:

```sh
read -rs 'NOTE_AGENT_TELEGRAM_TOKEN?Telegram test bot token: '
export NOTE_AGENT_TELEGRAM_TOKEN
export NOTE_AGENT_DATABASE_URL=postgresql://user:password@127.0.0.1:5432/note_agent
export NOTE_AGENT_PROCESSING_ENABLED=false
uv run --directory backend --locked python -m app.bot
```

Для одного токена запускайте один polling-процесс, без одновременно работающего
webhook. Бот не удаляет webhook и не сбрасывает ожидающие updates.
Обычный текст направляется в диалог T10. Пересланный текст сохраняется как заметка
с provenance и дедупликацией доставки.
Голос, файлы, подписи к медиа и редактирования сообщений здесь не обрабатываются.
Whitelist и авторизации нет: все чаты используют общую базу.

Для пересылок бот передаёт `title=None`. Общий `NotesService` выводит
автоматический заголовок из первой строки (до 200 символов), оставляет
`title_is_explicit=false` и сохраняет полный исходный текст.
`NOTE_AGENT_PROCESSING_ENABLED=true` включает
постановку одного process в транзакции сохранения; модели вызываются только
отдельным worker T08. По умолчанию обработка выключена.

В `sources` сохраняются канал telegram, числовой bot ID (область доставки),
update ID, chat/message ID и доступный `forward_origin` в metadata. Повтор update
возвращает ту же заметку без нового process, в том числе при конкурентной доставке.
Новый update с тем же текстом создаёт другую заметку. Повтор после удаления
не восстанавливает заметку и возвращает ошибку. Ошибка сохранения даёт сообщение
об ошибке; успех отправляется после commit. Если ответ Telegram не дошёл после
commit, повтор того же update безопасен. Ошибки сохранения не повторяются
автоматически: повторите отправку сообщения, если бот сообщил об ошибке.

Остановка — Ctrl-C или SIGTERM. Текущая прикладная операция завершается перед закрытием
пула, HTTP-сессия закрывается в finally, в том числе при ошибке запуска.
Для предотвращения утечки token/удалённых exception сообщения библиотечного
логгера aiogram подавлены; ошибка CLI выводит только фиксированный безопасный код
`telegram_bot_failed`. Импорт модулей не запускает сеть, БД или polling.

Постоянные тесты без Telegram-сети и моделей:

```sh
make lint test
sh scripts/test-postgres.sh -q
# Только Telegram SQL-сценарии с одноразовой БД:
sh scripts/test-postgres.sh -q -k telegram
```

Тесты используют синтетические aiogram updates и записывающую HTTP-сессию;
SQL-сценарии выполняются в отдельном контейнере без volumes. Реальную доставку
проверяйте отдельно только с предоставленным тестовым токеном и тестовым текстом.

## T13: обогащение в существующем process

После публикации индекса тот же handler опционально вызывает `TextGenerator.json`
с существующей DTO `Enrichment`. Результат повторно проверяется Pydantic;
summary, автоматические tags, category, entities и запись enrichments публикуются
атомарно через T06 (`notes → jobs`, token, действующая lease). Затем worker
завершает тот же job обычным механизмом T06. Модель вызывается после закрытия
соединения чтения, вне транзакций. Заголовок, manual_tags и подборки не меняются.

Обогащение по умолчанию выключено. Для включения добавьте в окружение **worker**:

```sh
export NOTE_AGENT_ENRICHMENT_ENABLED=true
export LLM_BASE_URL=http://127.0.0.1:11435/v1
export LLM_MODEL=qwen3:8b
uv run --directory backend --locked python -m app.postgres_worker
```

Остальные условия запуска, постановка новых заметок и backlog — в разделе T08.
`NOTE_AGENT_PROCESSING_ENABLED` управляет постановкой process в API;
`NOTE_AGENT_ENRICHMENT_ENABLED` управляет продолжением process в worker.
При выключенном обогащении LLMSettings и генератор не создаются: достаточно
embeddings. Включённый недоступный endpoint, неверный JSON/схема и превышение
бюджета дают `failed` с существующей безопасной ошибкой `handler_failed`, без
provider body и текста заметки. Записанный индекс и исходный текст остаются.
Явный retry повторяет индексирование и генерацию; chunks и provenance заменяются.
Автоматических повторов генерации и отдельных enrich jobs нет.

Собственные настройки (префикс `NOTE_AGENT_ENRICHMENT_`):

| Переменная | По умолчанию | Назначение |
|---|---:|---|
| `ENABLED` | false | Явное включение |
| `CONTEXT_TOKENS` | 8192 | Бюджет полного запроса и ответа |
| `RESPONSE_TOKENS` | 1024 | Передаётся адаптеру как max_tokens вместо LLM_MAX_TOKENS |
| `MAX_INPUT_BYTES` | 6000 | UTF-8 размер JSON с title и text |
| `MAX_OUTPUT_BYTES` | 8192 | UTF-8 размер сериализованной валидированной DTO |

Оценка входа консервативная: один UTF-8 байт за токен для byte-level tokenizer,
плюс system prompt, резерв 256 токенов на framing и RESPONSE_TOKENS.
Это не точный tokenizer; конфигурация должна соответствовать контексту endpoint.
Длинные заметки не обрезаются и не разбиваются на многоступенчатое summary:
после индекса получают failed при превышении бюджета. Ограничение ответа
передаётся серверу; дополнительно проверяется размер DTO перед публикацией.
Серверный лимит ответа или неверный формат не считаются успехом.

Provenance хранит точный настроенный `LLM_MODEL` и версию application prompt
`note-enrichment-v1`. OpenAI-compatible контракт не предоставляет проверку digest
генеративной модели: используйте стабильный идентификатор ревизии на endpoint.
Текст заметки передаётся отдельным JSON data record в user message;
system prompt запрещает выполнять инструкции из содержимого.
В карточке заметки отображаются четыре автоматических поля обычным React-текстом.

Постоянные проверки без весов/модельного runtime:

```sh
make lint test
sh scripts/test-postgres.sh -q
# Только SQL-сценарии T13, в отдельной одноразовой БД:
sh scripts/test-postgres.sh -q -k process_enrichment
cd frontend
npm ci --ignore-scripts
npm run check
```

Новые unit- и PostgreSQL-тесты автоматически входят в существующие Makefile/CI
команды. SQL-набор использует настоящую изолированную PostgreSQL/pgvector;
генерация детерминирована тестовым TextGenerator. Отдельный тест production
factory использует настоящий T07 JSON-mode HTTP-клиент и локальный тестовый
endpoint, в том числе с HTTP 503; это не smoke реальной модели.
Реальный JSON-mode smoke выполняйте отдельно командой generation из раздела T07,
после согласования свободного runtime. Переиспользуйте существующие веса/runtime;
не запускайте тяжёлые smoke одновременно с другими агентами.

## T14: правка, удаление и очистка

`PATCH /api/v1/notes/{id}` доступен при `ingestion_status=ready`. Под блокировкой
заметки сервис сохраняет новый заголовок и/или текст, отменяет queued/processing
jobs и обнуляет их lease/token. В той же транзакции удаляет chunks, enrichments
и семантические рёбра в обоих направлениях, сбрасывает автоматические поля
и ошибки. При `NOTE_AGENT_PROCESSING_ENABLED=true` создаётся один process.
Иначе статус обработки становится `not_requested`. Оригиналы, ручные теги
и подборки сохраняются. Полнотекстовый поиск видит сохранённую правку сразу,
семантический — после публикации нового индекса.

`DELETE /api/v1/notes/{id}` возвращает 204 и сразу скрывает заметку из CRUD
и поиска. Под той же блокировкой сервис отменяет активные jobs и ставит один
cleanup. Повторный DELETE при queued, processing или done возвращает 204
без новой постановки. При failed повторный DELETE переводит то же задание
в queued. Ошибка и число попыток хранятся в jobs. Для явного повтора доступен
также CLI `app.postgres_worker --retry JOB_UUID`.

Для worker укажите ту же `NOTE_AGENT_DATA_DIR`, в которой сохранены вложения.
Cleanup берёт ключи из attachments и удаляет только перечисленные файлы в
`DATA_DIR/uploads` через Storage, после закрытия соединения чтения. Удаление
отсутствующего файла считается успешным. При сбое ключи остаются в БД, поэтому
повторная попытка безопасно продолжает очистку. После удаления файлов worker
через проверку T06 очищает SQL-данные, включая исходный текст, производные
записи и членство в подборках. Строка заметки с `deleted_at` и история jobs
сохраняются. У Telegram-источника остаётся служебная запись с реквизитами
доставки, чтобы повтор update не создал заметку снова.

```sh
make lint test
sh scripts/test-postgres.sh -q -k note_lifecycle
make test-db
uv run --directory backend --locked --extra prototype pytest tests --ignore=tests/postgres
```

SQL-сценарии используют отдельный PostgreSQL/pgvector, синтетические файлы
во временных каталогах и детерминированные embeddings. Cleanup запускается
через production factory и существующий worker.

## T10. Веб-чат и диалог Telegram

Для нативного API и Telegram подготовьте PostgreSQL с миграциями T02/T09 и
доступный OpenAI-compatible endpoint T07 с native tool calling. В окружении
каждого процесса задайте `LLM_BASE_URL` и `LLM_MODEL`; используются и остальные
`LLM_*` настройки T07, включая timeout. Для гибридного поиска настройте
`EMBEDDING_*` по T09 и обработайте корпус через worker T08. При
`NOTE_AGENT_SEARCH_DENSE_ENABLED=false` поиск работает через PostgreSQL FTS,
а генеративный endpoint всё равно нужен. Подключение моделей для Compose требует
доступного из контейнера адреса и передачи настроек в окружение API. Для
локального чата используйте нативный запуск API из раздела «PostgreSQL и API».

```sh
export LLM_BASE_URL=http://127.0.0.1:11435/v1
export LLM_MODEL=qwen3:8b
export LLM_OPTIONS='{"temperature":0,"reasoning_effort":"none"}'
export EMBEDDING_BASE_URL=http://127.0.0.1:11435
curl --fail-with-body http://127.0.0.1:8000/api/v1/chat/messages \
  -H 'Content-Type: application/json' \
  -d '{"message":"Что есть в заметках про транзакции?"}'
```

Ответ содержит `conversation_id`, `text` и пустой `operations`. Передайте
`conversation_id` в следующем запросе для продолжения. Неизвестный веб-диалог
или неизвестная подборка при создании дают 404. Необязательный `collection_id`
при создании закрепляет подборку для истории диалога и ограничивает поиск.
Продолжение наследует подборку, а попытка передать другую получает 409.
В веб-интерфейсе откройте «Чат». Вкладка хранит ID в `sessionStorage`.
После перезагрузки модель продолжает серверный диалог. На экране отображаются
реплики, полученные после загрузки страницы. При ошибке введённый текст остаётся
в форме. Кнопка «Новый диалог» сбрасывает локальный ID.

В Telegram обычный текст идёт в тот же `ChatService.reply`. Диалог определяется
парой bot ID и chat ID. Повтор update возвращает сохранённый ответ, включая
случай параллельной доставки. После ошибки можно снова отправить вопрос.
При пересылке работает импорт T05 с прежними provenance и дедупликацией.

Перед моделью сервис читает ограниченную историю завершённых ходов. После
ответа короткая транзакция записывает сообщение пользователя, вызовы с
результатами в порядке протокола и финальную реплику. Если другая реплика
успела обновить тот же диалог, запрос возвращает `conversation_busy` (409) и
может быть повторён. Ошибки генерации и embeddings возвращают 503. Сбой
генерации или поиска оставляет историю в состоянии до запроса.

`search_notes` ищет через SearchService. `get_note` читает текущий текст через
NotesService по ID из результатов поиска данного хода. Тексты и превью
передаются модели в tool results как данные. При пустой выдаче без пригодного
контекста сервис сообщает о нехватке сведений. Оценка релевантности содержимого
и формулировка ответа при наличии результатов зависят от модели.

Переменные `NOTE_AGENT_CHAT_*`:

| Суффикс | По умолчанию | Назначение |
|---|---:|---|
| `CONTEXT_TOKENS` | 8192 | Бюджет входа и ответа, согласуйте с контекстом endpoint |
| `RESPONSE_TOKENS` | 512 | `max_tokens` запроса чата, переопределяет `LLM_MAX_TOKENS` |
| `MAX_RESPONSE_BYTES` | 3600 | UTF-8 лимит итогового ответа для обоих транспортов |
| `MAX_STEPS` | 4 | Вызовы модели за один ход, включая финальный |
| `MAX_TOOL_CALLS` | 6 | Суммарный лимит вызовов инструментов за ход |
| `HISTORY_MESSAGES` | 40 | Сколько строк истории читать; неполные ходы отбрасываются |
| `TOOL_RESULT_BYTES` | 1800 | UTF-8 лимит JSON каждого результата |
| `SEARCH_LIMIT` | 5 | Максимум кандидатов за вызов поиска |

Оценка входа использует UTF-8 размер сериализованных сообщений и схем,
запас на обрамление и резерв ответа. Точный tokenizer зависит от endpoint.
При переполнении сначала убираются старые ходы, результаты поиска и текст
чтения ограничиваются бюджетом. Слишком длинный запрос получает 422,
слишком длинный ответ модели даёт ошибку. Последний шаг генерации выполняется
без инструментов. Некорректные аргументы в JSON-объекте получают парный
tool result с ошибкой. Некорректный wire-ответ провайдера даёт 503.

```sh
make lint test
make test-db
npm --prefix frontend ci --ignore-scripts
npm --prefix frontend run check
```

Отдельный smoke выполняется при свободном runtime с существующими весами
Qwen3 и Qwen3 Embedding (см. запуск Ollama в разделе модельных клиентов).
Проверка использует одноразовый PostgreSQL и синтетическую заметку:

```sh
NOTE_AGENT_CHAT_SMOKE=1 LLM_BASE_URL=http://127.0.0.1:11435/v1 \
  EMBEDDING_BASE_URL=http://127.0.0.1:11435 \
  LLM_OPTIONS='{"temperature":0,"seed":42,"reasoning_effort":"none"}' \
  sh scripts/test-postgres.sh -q -s -k chat_real_endpoint
```

## T11: оригиналы и TXT/Markdown

`POST /api/v1/notes/import` принимает multipart: `source_type=text_file`,
ровно один `file`, необязательный `title`. Поддерживаются расширения `.txt`,
`.md`, `.markdown` без учёта регистра. MIME клиента не доказывает формат:
worker строго декодирует содержимое как UTF-8, допускается UTF-8 BOM.
UTF-16, Windows-1251 и другие кодировки не поддерживаются: предварительно
преобразуйте их в UTF-8. Ошибки декодирования не заменяются символом замены.
NUL и управляющие символы кроме TAB/CR/LF отклоняются как бинарное содержимое.
Markdown сохраняется обычным текстом с исходными заголовками, абзацами,
пробелами и переводами строк; BOM исключается только из extracted_text.

```sh
curl --fail -X POST http://127.0.0.1:8000/api/v1/notes/import \
  -F source_type=text_file -F 'file=@/tmp/notes.md' -F 'title=Конспект'
curl --fail http://127.0.0.1:8000/api/v1/attachments/ATTACHMENT_UUID/download \
  --output /tmp/original.md
curl --fail -X POST http://127.0.0.1:8000/api/v1/notes/NOTE_UUID/retry \
  -H 'Content-Type: application/json' -d '{"stage":"extract"}'
```

Импорт возвращает `202` и полную карточку с queued ingestion. Без title заголовок
берётся из последнего компонента имени файла (до 200 символов),
`title_is_explicit=false`; явный title защищён от автоматического обогащения.
Пользовательское имя хранится в метаданных вложения. Служебный ключ
`<attachment UUID>/original` не зависит от него и не возвращается API.
Storage сохраняет исходные байты до открытия SQL-транзакции; note/source/attachment
и extract job создаются одной транзакцией. При ошибке SQL оригинал удаляется,
при ошибке записи временный файл удаляется самим Storage.

`NOTE_AGENT_TEXT_FILE_MAX_BYTES` по умолчанию 1048576 (1 MiB),
`NOTE_AGENT_TEXT_MAX_CHARS` — 100000; допускается уменьшение лимитов.
Пустой файл отклоняется при загрузке (`422`, `empty_file`), превышение байтового
лимита — `413`, неподдерживаемое расширение или source_type — `422`.
Пустой после декодирования текст, бинарное содержимое, неверная кодировка и
превышение числа символов переводят ingestion в failed с безопасными code/message.
Оригинал остаётся доступен. Текст не обрезается; ограничение заголовка не затрагивает
содержимое файла. Retry возвращает `202`, использует тот же failed job, снимает
ошибку и не создаёт дубликатов; повтор при queued/processing/ready даёт `409`.

Один worker запускается через `app.postgres_worker`, default factory —
`app.indexing:handlers`. Она регистрирует все три обработчика; process сохраняет
индексирование и обогащение T08/T13, cleanup — очистку T14. Extract читает
метаданные через autocommit и закрывает соединение перед чтением и декодированием
файла. Writer делает только SQL под защитой note → job, token/lease и deleted_at.
При включённом `NOTE_AGENT_PROCESSING_ENABLED` process ставится в той же транзакции
публикации; иначе остаётся not_requested. До ready PATCH даёт `409`; DELETE
отменяет extract, и старый результат не публикуется. Истёкшая lease также запрещает
публикацию. Download возвращает `404` для удалённой заметки или отсутствующего файла.

Нативные API и worker используют одинаковый абсолютный `NOTE_AGENT_DATA_DIR`.
В Compose оригиналы хранятся в `<compose-project>_originals-data`, `/app/data`
доступен пользователю контейнера. Для запуска единственного worker в Compose
с теми же БД и volume используйте отдельный терминал:

```sh
docker compose run --rm --no-deps api python -m app.postgres_worker
```

Не запускайте одновременно второй нативный worker для этой БД. API и worker
должны получать одинаковое значение processing_enabled. Тесты входят в постоянные
`make lint test`, `make test-db` и `sh scripts/check-backend.sh`:

```sh
sh scripts/test-postgres.sh -q -k file_import
uv run --directory backend --locked --extra prototype pytest tests --ignore=tests/postgres
```

SQL-сценарии используют изолированный PostgreSQL/pgvector и временный Storage;
embeddings и генерация — тестовые адаптеры. Compose smoke использует новые volumes,
проверяет upload/download и сохранность оригинала после перезапуска без моделей.

## T12: пачки файлов и подборки

В веб-интерфейсе откройте «Загрузка» для одного или нескольких TXT/Markdown
и «Подборки» для просмотра и управления заметками. Ссылка на результаты импорта
содержит ID пачки. ID из ответа Telegram можно ввести на странице «Загрузка».
Опрос результатов идёт каждые две секунды для активной пачки на видимой странице.
Из карточки заметки с ошибкой можно запросить повтор извлечения.

```sh
curl --fail -X POST http://127.0.0.1:8000/api/v1/imports \
  -F 'files=@/tmp/notes.md' -F 'files=@/tmp/lecture.txt' -F 'collection_title=Лекции'
curl --fail http://127.0.0.1:8000/api/v1/imports/BATCH_UUID
curl --fail 'http://127.0.0.1:8000/api/v1/collections/COLLECTION_UUID?limit=20&offset=0'
```

В multipart можно указать `collection_title` для новой подборки или
`collection_id` для существующей. Если оба поля опущены, создаётся отдельная
подборка «Импорт ДД.ММ.ГГГГ» с датой UTC. HTTP-лимиты: 20 файлов и 10 МиБ
файловых данных, плюс до 128 КиБ для multipart-обёртки. Прежний лимит каждого
файла применяется отдельно. Сбой загрузки, валидации или извлечения виден
у конкретного файла. Для неудачного приёма файла заметка ещё не создана.

`POST /collections` создаёт подборку (201), `GET /collections` возвращает массив.
В деталях пагинируются живые заметки. Добавление через
`POST /collections/{id}/notes` принимает `{"note_ids":["UUID"]}`, повторное
добавление идемпотентно. Исключение из подборки и её удаление возвращают 204.
Заметки остаются доступны после удаления подборки.

`202` от импорта означает сохранённые результаты приёма. Извлечение выполняет
имеющийся последовательный worker T11/T14. Прогресс вычисляется при GET из
результатов приёма и состояния извлечения по [контракту API](architecture.md#пачки-подборки-поиск-чат-и-граф).
После удаления заметки item скрывает её ID, сохраняя завершённый исход
извлечения. Прерванное удалением извлечение даёт `note_deleted`.
Удалённая подборка отображается как `collection_id: null`.

Telegram принимает документы через тот же импорт. Для одного документа
создаётся пачка из одного item. Группа собирается до одной секунды от первого
документа или до 10 документов. Идентичность группы включает ID бота и чата.
Поздние документы дополняют ту же пачку и могут снова перевести её в активное
состояние. Уже записанные пачки доступны после перезапуска бота. Если процесс
аварийно завершится до записи группы, содержимое окна сбора потеряется.
Повтор доставки возвращает ID уже принятой пачки. При штатной остановке
собранные группы сбрасываются в импорт до закрытия пула и Telegram-сессии.
Лимит активных групп — 16, скачивание каждого документа ограничено по времени
и байтам. Обычный текст направляется в диалог T10, пересылки текста — в T05.

```sh
sh scripts/test-postgres.sh -q -k import_batches
npm --prefix frontend run check
```

## T17. Семантический граф

При публикации индекса PostgreSQL вычисляет среднее арифметическое embeddings
по всем chunks каждой живой готовой заметки. Усреднение идёт в порядке chunks,
все chunks имеют равный вес. Score — косинусная близость двух средних
векторов. Нулевые средние векторы пропускаются. Используется единственный
профиль embeddings в БД, который T08 проверяет и блокирует до конца публикации.
При административной смене профиля очистите также `note_links` вместе с
`chunks` и переиндексируйте корпус.

Для worker действуют две переменные:

| Переменная | По умолчанию | Диапазон |
|---|---:|---|
| `NOTE_AGENT_GRAPH_TOP_K` | 5 | 1–100 |
| `NOTE_AGENT_GRAPH_MIN_SCORE` | 0.75 | от −1 до 1 включительно |

Для публикуемой заметки выбираются до top-k соседей со score не ниже порога.
Порядок кандидатов — score по убыванию, UUID по возрастанию. Один выбранный
сосед даёт одно неориентированное ребро с упорядоченными UUID и источником
`embedding-centroid-cosine-v1`. Публикация заменяет все рёбра своей заметки,
включая ранее созданные соседями. Последняя публикация одного из двух концов
решает, остаётся ли ребро. Узел может иметь больше top-k соседей за счёт
последующих публикаций других заметок.

Чтобы построить граф для ранее проиндексированного корпуса, с теми же
`NOTE_AGENT_DATABASE_URL` и настройками embeddings, что у worker, выполните:

```sh
uv run --directory backend --locked python -m app.services.processing
```

Команда ставит обычный `process` для живых заметок с готовым текстом, включая
уже проиндексированные. Worker заново строит индекс и связи. Активные задания
и заметки с failed обработкой пропускаются. Для последних используйте явный
`retry` стадии `process` из раздела T08. Запуск API сам не ставит задания.

Индекс и рёбра фиксируются в одной публикации T06 до обогащения. Выбранные
соседи удерживаются через `FOR SHARE SKIP LOCKED`: занятые конкурентной
правкой кандидаты пропускаются, а перед вставкой повторно проверяется наличие
их chunks. PATCH удаляет рёбра в обоих направлениях. DELETE сразу скрывает
узел и рёбра, физическую очистку выполняет cleanup. Сбой обогащения сохраняет
опубликованные индекс и граф. Точный расчёт средних векторов сканирует chunks
при каждой публикации. Для этого шага применяется lease worker.

`GET /api/v1/graph` принимает `note_id`,
`collection_id` и `limit` (число узлов, от 1 до 100, по умолчанию 20).
С `note_id` сначала идёт сама заметка, затем её прямые соседи по убыванию
score и возрастанию UUID. Общий граф отдаёт приоритет концам самых сильных
рёбер (при равенстве score — по паре UUID). При фильтре по подборке оба конца
ребра должны входить в неё. В ответ входят все сохранённые рёбра между
выбранными узлами. Отсутствующая, удалённая или отфильтрованная начальная
заметка даёт пустой граф.

В веб-интерфейсе раздел «Граф» открывает общий граф. Ссылка «Окружение
заметки» в карточке с готовым текстом открывает её прямых соседей. Список
«Подборка» ограничивает оба режима, а клик по узлу открывает карточку. Экран
запрашивает до 20 узлов.

```sh
sh scripts/test-postgres.sh -q -k test_graph
npm --prefix frontend ci --ignore-scripts
npm --prefix frontend run check
```
