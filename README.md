# URL Shortener

This project provides a core URL shortener with FastAPI, PostgreSQL, SQLAlchemy and Alembic. Python 3.12+ and Docker Desktop with Compose are required; PostgreSQL need not be installed locally.

## 1. Structure

- `app/main.py` creates the API and registers routes.
- `app/core/` contains settings shared by the API and migrations.
- `app/db/` contains the declarative base, engine, and session dependency.
- `app/models/` describes database tables. Import new models in `app/models/__init__.py` so Alembic sees them.
- `app/schemas/` contains Pydantic input and output definitions.
- `app/api/routes/` groups HTTP endpoints.
- `app/services/` contains URL business logic; `app/utils/` contains the independent random code generator.
- `alembic/env.py` connects migrations to settings and model metadata; `alembic/versions/` contains schema changes.

## 2. Configuration and dependencies

In PowerShell from this directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Use any installed Python 3.12 or newer if `py -3.12` is unavailable. Set a local password in `.env` before starting the database. Git ignores `.env`; `.env.example` contains sample values only. Existing environment variables take precedence over `.env`. Do not use the sample password in production.

`get_database_url()` uses `URL.create` to construct a SQLAlchemy URL. `postgresql+psycopg` selects PostgreSQL and the Psycopg 3 driver; the host, port, database, user and password identify the server and credentials. A URL object safely handles special characters in passwords. `localhost` is correct for Python running on the host; a future app container on the Compose network would use `db`. The engine connects lazily on first use.

## 3. PostgreSQL with Docker

Start Docker Desktop and wait for it to be ready:

```powershell
docker compose up -d
docker compose ps
docker compose logs db
```

The service binds to localhost on `POSTGRES_PORT` (default 5432). Its health check uses `pg_isready`, and the named volume persists data across container recreation. Stop and restart it with:

```powershell
docker compose stop
docker compose start
```

`docker compose down` removes the container and network but retains the volume. The image uses its initialization credentials only when the volume is first created: editing credentials in `.env` later does not update an existing database. Update the database separately if you change them. The local port must be free.

## 4. SQLAlchemy connections

- **Engine:** the application-wide entry point and connection pool; `pool_pre_ping=True` checks connections before reuse.
- **Connection:** an actual checked-out DBAPI connection used to communicate with PostgreSQL.
- **Session:** a short-lived ORM unit of work that uses connections and tracks a transaction and mapped objects.
- **Base:** the declarative parent collecting model definitions in `Base.metadata`.
- **Model:** a Python class mapped to a table, such as `URL`.

`SessionLocal` creates sessions bound to the engine. FastAPI calls `get_db()` to yield a session and closes it after the request, even on errors. Dependency injection makes the lifetime explicit and allows tests to substitute the dependency. A future write endpoint must explicitly commit; closing an uncommitted session rolls it back.

## 5. URL model and schema

`app/models/url.py` maps `URL` to `urls`. Integer `id` is the primary key; `original_url` is required and limited to 2048 characters; `short_code` is required, indexed and unique. `created_at` defaults to PostgreSQL's `now()`; `expires_at` can be null. Both timestamps use PostgreSQL's timezone-aware type. `Mapped` and `mapped_column` tell SQLAlchemy the Python and database types.

`URLCreate` in `app/schemas/url.py` uses Pydantic `HttpUrl` to validate an HTTP(S) URL, and also checks the normalized value fits the database column. `URLResponse` describes the result. An ORM model represents stored database rows and constraints; a Pydantic schema validates and serializes API data.

## 6. Migrations

With the database healthy and the virtual environment activated:

```powershell
python -m alembic upgrade head
python -m alembic current
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\d urls"'
```

The initial migration creates the table and unique index. `alembic/env.py` imports `app.models` to register its tables with `Base.metadata`, and reads the same settings as the API. For a future model change:

```powershell
python -m alembic revision --autogenerate -m "describe change"
python -m alembic upgrade head
```

`revision --autogenerate` compares registered metadata to the live database and writes a candidate migration. Review and edit it before applying: autogeneration cannot detect every change. `upgrade head` applies outstanding migrations. `downgrade -1` reverses one revision and can destroy data, so review it first. Alembic tracks the applied revision in `alembic_version`. This project does not call `create_all()`.

Migration flow: **SQLAlchemy model → Alembic revision → PostgreSQL schema**. The checked-in initial revision was authored explicitly; check it with `python -m alembic check` once applied.

## 7. Run the API

```powershell
python -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for interactive documentation. `GET /` identifies the API, `GET /health` checks the process, and `GET /health/db` issues `SELECT 1` and returns HTTP 503 when the database is unavailable. A successful database health check proves connectivity, not that migrations are current.

Request flow: **HTTP request → FastAPI route → injected SQLAlchemy Session → pooled Connection / Psycopg → PostgreSQL → response**. The root and basic health routes do not require a database connection.

## Core shortener

Set `BASE_URL` in `.env` to the public HTTP(S) base address users should visit, for example `http://localhost:8000` locally. The service uses this configured address instead of trusting the request's Host header. Include a path prefix only if your reverse proxy serves the app under that prefix. Restart the API after changing the value.

`POST /api/v1/urls` validates and normalizes `original_url`, then calls `create_short_url`. The generator chooses seven characters with `secrets.choice` from uppercase letters, lowercase letters, and digits: this 62-character alphabet is called Base62. Random codes are hard to predict, but randomness alone cannot guarantee uniqueness. PostgreSQL's unique index does that. A check before insertion cannot guarantee uniqueness because two concurrent requests can both see a code as available.

The service calls `db.add(record)` to stage a model, `db.commit()` to insert it and commit the transaction, then `db.refresh(record)` to load database-generated values such as the ID. A collision raises `IntegrityError`: the service rolls back the failed transaction and retries, up to five attempts. Only PostgreSQL unique violations for `ix_urls_short_code` are retried; unrelated integrity errors propagate. Exhausting attempts returns HTTP 503.

`GET /{short_code}` calls the lookup service. A missing code returns 404 and an expired record returns 410. Otherwise HTTP 307 includes a `Location` header; a browser follows that address using the original request method. The service keeps database and collision logic out of route functions. The complete flow is **client → FastAPI router → service → SQLAlchemy session → PostgreSQL**, then back to the client. No click tracking is performed.

### Custom aliases and expiration

You can optionally add `custom_alias` and `expires_at` to the same POST request:

```json
{
  "original_url": "https://github.com",
  "custom_alias": "mygithub",
  "expires_at": "2099-12-31T23:59:00Z"
}
```

An alias must contain 3–30 ASCII letters, digits, hyphens or underscores; it must start and end with a letter or digit. Aliases preserve case, so `MyLink` and `mylink` are different. Names reserved for application routes, such as `docs`, `redoc`, `api`, and `health`, are rejected case-insensitively with 422. The full set is in `app/core/aliases.py`. A duplicate alias returns 409. Omitting an alias keeps the existing seven-character random code behavior; both kinds of code share the same unique index. Expired links retain their aliases and cannot be reused.

`expires_at` must be a future ISO 8601 timestamp with an explicit timezone, such as `Z` or `+05:30`. Omit it or send `null` for no expiration. Pydantic converts an accepted timestamp to UTC; PostgreSQL stores the instant as a timezone-aware timestamp. Local display time may differ depending on the viewer's timezone, but it represents the same instant. At or after expiration the link returns 410 Gone, meaning it existed but is no longer available; a code that never existed returns 404. PostgreSQL does not delete expired records.

Validation belongs in the Pydantic schema so every request to this endpoint follows the same rules. The unique index remains the final authority: two requests can simultaneously see an alias as free, but only one insert can succeed. The service rolls back the other transaction and reports 409; it never silently replaces the requested alias. The existing nullable `expires_at` column and unique index already support this behavior, so no migration was needed. A future schema change would require a reviewed Alembic migration.

#### Try Stage 3

With the API running, open `/docs`, choose **POST /api/v1/urls**, click **Try it out**, and submit the JSON above. Expect 201; submitting the same alias again returns 409. Replace the alias with `docs` to see 422. Use a new alias each time if the previous one already exists. Omitting the two optional fields still creates a Stage 2 link.

For a PowerShell and curl example, save a request and submit it (replace the alias if already used):

```powershell
'{"original_url":"https://github.com","custom_alias":"mygithub"}' | Set-Content -Encoding ascii request.json
curl.exe -i -H "Content-Type: application/json" --data-binary '@request.json' http://localhost:8000/api/v1/urls
curl.exe -i http://localhost:8000/mygithub
curl.exe -i http://localhost:8000/no-such-alias
```

The valid link returns 307 and the missing one returns 404. For a 410 test, create a **disposable** alias with a near-future expiration, wait until that time has passed, and request its short URL again. A link created with an offset timestamp can be inspected using the PostgreSQL query below; the displayed timezone depends on the PostgreSQL session setting.

The standard test suite uses SQLite and simulated PostgreSQL errors. To additionally run the PostgreSQL integration test against a disposable temporary schema, set `TEST_POSTGRES_URL` to a separate test database connection URL and run `python -m pytest -q -p no:cacheprovider tests/test_postgres_integration.py`. The database user needs permission to create and drop schemas. The test creates a uniquely named schema, runs the initial migration there, tests concurrent requests and collisions, and removes that schema afterward. It does not modify existing tables or URL records.

To try the API, start PostgreSQL, apply migrations, and run Uvicorn as above. Open `http://127.0.0.1:8000/docs` and use `POST /api/v1/urls`, or run in another PowerShell terminal:

```powershell
$created = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/urls -ContentType 'application/json' -Body '{"original_url":"https://www.google.com"}'
$created
curl.exe -i $created.short_url
Start-Process $created.short_url
curl.exe -i http://localhost:8000/nonexistent-code
```

The first `curl.exe` shows `307` and `Location`; opening the URL in a browser follows the redirect. Inspect records using the credentials and database in your `.env` (the following use the example names):

```powershell
docker compose exec db psql -U shortener -d shortener -c "SELECT id, original_url, short_code, expires_at FROM urls;"
```

To observe 410, **on a disposable local record**, replace `1` below with the ID returned by the POST and then request its short URL again:

```powershell
docker compose exec db psql -U shortener -d shortener -c "UPDATE urls SET expires_at = now() - interval '1 minute' WHERE id = 1;"
curl.exe -i $created.short_url
```

The default automated tests use a disposable in-memory SQLite schema and simulated PostgreSQL collision errors. The opt-in PostgreSQL integration test is skipped unless `TEST_POSTGRES_URL` is set. Run the default suite with:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q -p no:cacheprovider
```

## 8. Verify

With Docker running, migrations applied, and Uvicorn running in another terminal:

```powershell
docker compose ps
python -m alembic current
python -m alembic check
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\d urls"'
Invoke-RestMethod http://127.0.0.1:8000/
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/health/db
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/docs).StatusCode
```

The container should be healthy, `current` should show revision `0001`, `check` should report no new operations, `\d urls` should show the columns and unique index, the API requests should succeed, and `/docs` should return 200.

python -m uvicorn app.main:app --reload
