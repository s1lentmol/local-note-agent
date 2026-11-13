# Local Note Agent

Локальное приложение для заметок и личной базы знаний, находящееся в разработке.
Цель проекта — собирать материалы через веб и Telegram, искать их по смыслу
и получать ответы с помощью сменяемых языковых моделей.

Целевой стек: Python, FastAPI, PostgreSQL с pgvector, Psycopg 3,
React и TypeScript. Архитектура предусматривает отдельные HTTP-адаптеры моделей.

## Быстрый старт

Нужны Docker с Compose v2 (`--wait`), Python 3.11 и
[uv](https://docs.astral.sh/uv/getting-started/installation/) в PATH.

```sh
cp .env.example .env
# Замените пароль в POSTGRES_PASSWORD и NOTE_AGENT_DATABASE_URL в .env.
make setup
make up
curl --fail http://127.0.0.1:8000/api/v1/health
make down
```

Команды запускают PostgreSQL/pgvector и API на `127.0.0.1:8000`.
Документация API доступна на [127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).
`make down` сохраняет данные PostgreSQL.

## Документация

- [Разработка и запуск](docs/development.md)
- [Архитектура и контракты](docs/architecture.md)
- [Состояние реализации и ограничения](docs/work-plan.md)
- [Организация совместной разработки](docs/team_distribution.md)
- [Правила для кодинговых агентов](AGENTS.md)
