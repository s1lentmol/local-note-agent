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
docker compose up -d --wait db
docker compose build api
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

`make test` проверяет API/DTO отдельно; `make test-db` обязателен для схемы,
мигратора и пула. CI выполняет обе команды. Полный локальный набор:

```sh
make lint
make test
make test-db
uv run --directory backend --locked --extra prototype pytest tests --ignore=tests/postgres
```

DTO описывают контракты, маршруты появятся в следующих задачах. Идентичность
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

В T06 настоящих extract/process/cleanup обработчиков нет. Без factory worker
ставит занятому job `failed` с `handler_missing`; фиктивного успеха нет.
`--retry UUID` явно переводит только подходящий `failed` job в `queued` и выходит:

```sh
uv run --directory backend --locked python -m app.postgres_worker --retry JOB_UUID
```

Это CLI и метод `JobsRepository.retry`, HTTP-маршрут остаётся в области T03.
Retry сохраняет attempts; новая попытка увеличивает attempts и получает новый
UUID token. При активном job того же вида, удалённой заметке для extract/process
или другом статусе retry возвращает отказ (CLI exit 1). Cleanup, наоборот,
допускается только для служебной записи с `deleted_at`.

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
