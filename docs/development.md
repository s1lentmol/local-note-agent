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
