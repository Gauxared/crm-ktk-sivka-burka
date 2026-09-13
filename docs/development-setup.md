# Локальная среда разработки

ENV-001 создаёт каркас общего backend для лендинга, CRM и будущих адаптеров ботов.
Он пока содержит только `/health`; PostgreSQL не подключается, миграции и бизнес-маршруты
ещё не реализованы. Первый вертикальный сценарий будет отдельной задачей.

## Зафиксированный стартовый стек

- Python 3.12.
- FastAPI 0.141.1 и Uvicorn 0.52.4.
- SQLAlchemy 2.0.52, Psycopg 3.3.5 и Alembic 1.20.0.
- PostgreSQL 17 через Docker Compose, именованный volume `sivka_postgres_data`.

Версии Python-пакетов закреплены в `pyproject.toml`; они устанавливаются только в `.venv`
проекта. Образ PostgreSQL пока закреплён на поддерживаемой major-линии `17-alpine`.
Перед общим окружением или пилотом его нужно заменить на проверенный неизменяемый digest.
Это исключает тихую смену образа, но не требует скачивать контейнер в ENV-001.

## Подготовка

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

`.env` игнорируется Git. В примере только development-значения; реальные секреты,
адреса ботов и данные владельца туда не добавлены. Production требует `DATABASE_URL`,
`SESSION_SECRET` и `ADMIN_BOOTSTRAP_LOGIN`, иначе процесс завершается с ошибкой настроек.

## Проверки без БД

```powershell
.\.venv\Scripts\python -m pytest tests/api -q
.\.venv\Scripts\python -m uvicorn sivka_burka_api.main:app --app-dir apps/api --port 8000
```

После запуска `GET http://127.0.0.1:8000/health` возвращает liveness процесса.
Это не healthcheck PostgreSQL и не готовый публичный endpoint для клиента.

## PostgreSQL

Перед стартом проверьте, что Docker Desktop работает и у Windows есть устойчивый запас
оперативной памяти. В момент ENV-001 после запуска Docker свободная память опускалась
ниже 1 GiB; контейнер не запускается автоматически. PostgreSQL ограничен 384 MiB и 1 CPU,
но это не гарантирует запуск при нехватке памяти на хосте.

```powershell
docker compose --env-file .env up -d postgres
docker compose --env-file .env ps
docker compose --env-file .env logs postgres
docker compose --env-file .env down
```

Данные находятся в Docker volume, не в OneDrive и не в Git. `down` останавливает контейнер;
`down --volumes` удалит development-данные и должен использоваться только осознанно.
Перед выполнением будущих миграций проверим `pg_isready`, подключение и восстановление
из тестовой копии. Не подключайте существующую БД и не используйте production credentials.
