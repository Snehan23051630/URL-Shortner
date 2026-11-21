import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# The app creates its engine at import; tests override its session dependency.
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")

from app.core.config import get_base_url
from app.db.session import get_db
from app.main import app
from app.models.url import URL
from app.services import url_service
from app.utils.short_code import ALPHABET, generate_short_code


@pytest.fixture
def db_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    # A disposable test schema; PostgreSQL migrations are verified separately.
    with engine.begin() as conn:
        conn.execute(text("""CREATE TABLE urls (
            id INTEGER PRIMARY KEY, original_url VARCHAR(2048) NOT NULL,
            short_code VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME
        )"""))
        conn.execute(text("CREATE UNIQUE INDEX ix_urls_short_code ON urls (short_code)"))
    yield engine
    engine.dispose()


@pytest.fixture
def client(db_engine):
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    # BASE_URL is obtained through settings directly, not dependency injection.
    get_base_url.cache_clear()
    original = os.environ.get("BASE_URL")
    os.environ["BASE_URL"] = "https://sho.rt"
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    if original is None:
        os.environ.pop("BASE_URL", None)
    else:
        os.environ["BASE_URL"] = original
    get_base_url.cache_clear()


def pg_integrity_error(sqlstate="23505", constraint="ix_urls_short_code"):
    original = Exception("simulated database error")
    original.sqlstate = sqlstate
    original.diag = SimpleNamespace(constraint_name=constraint)
    return IntegrityError("INSERT", {}, original)


def test_generator_is_base62():
    code = generate_short_code()
    assert len(code) == 7
    assert set(code) <= set(ALPHABET)
    assert len(generate_short_code(12)) == 12


def test_create_and_redirect(client, db_engine):
    response = client.post("/api/v1/urls", json={"original_url": "https://www.google.com"})
    assert response.status_code == 201
    body = response.json()
    assert body["original_url"] == "https://www.google.com/"
    assert body["short_url"] == f"https://sho.rt/{body['short_code']}"
    with Session(db_engine) as db:
        assert db.get(URL, body["id"]).short_code == body["short_code"]
    redirect = client.get(f"/{body['short_code']}", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "https://www.google.com/"


@pytest.mark.parametrize("url", ["not a url", "ftp://example.com", "javascript:alert(1)", "https://example.com/" + "a" * 2048])
def test_invalid_url(client, url):
    assert client.post("/api/v1/urls", json={"original_url": url}).status_code == 422


def test_missing_and_expired(client, db_engine):
    assert client.get("/missing", follow_redirects=False).status_code == 404
    with Session(db_engine) as db:
        db.add(URL(original_url="https://example.com/", short_code="expired", expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        db.commit()
    assert client.get("/expired", follow_redirects=False).status_code == 410


def test_unexpired_url_redirects(client, db_engine):
    with Session(db_engine) as db:
        db.add(URL(original_url="https://example.com/", short_code="future1", expires_at=datetime.now(timezone.utc) + timedelta(minutes=1)))
        db.commit()
    assert client.get("/future1", follow_redirects=False).status_code == 307


def test_collision_retries_and_rolls_back(client, monkeypatch):
    codes = iter(["used123", "fresh12"])
    monkeypatch.setattr(url_service, "generate_short_code", lambda: next(codes))
    real_commit, real_rollback = Session.commit, Session.rollback
    calls = {"commit": 0, "rollback": 0}

    def commit(db):
        calls["commit"] += 1
        if calls["commit"] == 1:
            raise pg_integrity_error()
        return real_commit(db)

    def rollback(db):
        calls["rollback"] += 1
        return real_rollback(db)

    monkeypatch.setattr(Session, "commit", commit)
    monkeypatch.setattr(Session, "rollback", rollback)
    response = client.post("/api/v1/urls", json={"original_url": "http://example.com"})
    assert response.status_code == 201
    assert response.json()["short_code"] == "fresh12"
    assert calls == {"commit": 2, "rollback": 1}


def test_collision_exhaustion_returns_503(client, monkeypatch):
    calls = []
    def fail(db):
        calls.append(1)
        raise pg_integrity_error()
    monkeypatch.setattr(Session, "commit", fail)
    response = client.post("/api/v1/urls", json={"original_url": "https://example.com"})
    assert response.status_code == 503
    assert len(calls) == 5


@pytest.mark.parametrize("sqlstate,constraint", [("23505", "another_index"), ("23502", "ix_urls_short_code")])
def test_unrelated_integrity_errors_are_not_retried(client, monkeypatch, sqlstate, constraint):
    calls = []
    def fail(db):
        calls.append(1)
        raise pg_integrity_error(sqlstate, constraint)
    monkeypatch.setattr(Session, "commit", fail)
    response = client.post("/api/v1/urls", json={"original_url": "https://example.com"})
    assert response.status_code == 500
    assert len(calls) == 1


def test_database_insertion_failure_is_not_retried(client, monkeypatch):
    calls = []
    def fail(db):
        calls.append(1)
        raise OperationalError("INSERT", {}, Exception("database unavailable"))
    monkeypatch.setattr(Session, "commit", fail)
    response = client.post("/api/v1/urls", json={"original_url": "https://example.com"})
    assert response.status_code == 500
    assert len(calls) == 1


def test_custom_alias_and_case_sensitivity(client):
    for alias in ("myGithub", "mygithub"):
        result = client.post("/api/v1/urls", json={"original_url": "https://github.com", "custom_alias": alias})
        assert result.status_code == 201
        assert result.json()["short_code"] == alias
        assert result.json()["expires_at"] is None
        assert client.get(f"/{alias}", follow_redirects=False).status_code == 307


@pytest.mark.parametrize("alias", ["", "ab", "a" * 31, "has space", "bad!alias", "-start", "_start", "end-", "end_", "docs", "DoCs", "redoc", "api", "health", "openapi.json"])
def test_invalid_aliases(client, alias):
    response = client.post("/api/v1/urls", json={"original_url": "https://example.com", "custom_alias": alias})
    assert response.status_code == 422


@pytest.mark.parametrize("alias", ["abc", "a-b", "a_b", "A" * 30])
def test_valid_alias_boundaries(client, alias):
    response = client.post("/api/v1/urls", json={"original_url": "https://example.com", "custom_alias": alias})
    assert response.status_code == 201
    assert response.json()["short_code"] == alias


def test_custom_collision_returns_409_without_retry(client, monkeypatch):
    calls = []
    def fail(db):
        calls.append(1)
        raise pg_integrity_error()
    monkeypatch.setattr(Session, "commit", fail)
    response = client.post("/api/v1/urls", json={"original_url": "https://example.com", "custom_alias": "taken"})
    assert response.status_code == 409
    assert len(calls) == 1


def test_custom_other_integrity_error_is_not_translated(client, monkeypatch):
    def fail(db):
        raise pg_integrity_error("23505", "another_index")
    monkeypatch.setattr(Session, "commit", fail)
    response = client.post("/api/v1/urls", json={"original_url": "https://example.com", "custom_alias": "alias"})
    assert response.status_code == 500


def test_generated_code_skips_reserved_name(client, monkeypatch):
    codes = iter(["openapi", "Abc1234"])
    monkeypatch.setattr(url_service, "generate_short_code", lambda: next(codes))
    response = client.post("/api/v1/urls", json={"original_url": "https://example.com"})
    assert response.status_code == 201
    assert response.json()["short_code"] == "Abc1234"


def test_future_expiration_normalized_to_utc(client):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(timezone(timedelta(hours=5, minutes=30)))
    response = client.post("/api/v1/urls", json={"original_url": "https://example.com", "expires_at": future.isoformat()})
    assert response.status_code == 201
    actual = datetime.fromisoformat(response.json()["expires_at"].replace("Z", "+00:00"))
    assert actual.utcoffset() == timedelta(0)
    assert actual == future
    assert client.get("/" + response.json()["short_code"], follow_redirects=False).status_code == 307


@pytest.mark.parametrize("expiration", ["2000-01-01T00:00:00Z", "2099-01-01T00:00:00", 4102444800, "invalid"])
def test_invalid_expiration(client, expiration):
    response = client.post("/api/v1/urls", json={"original_url": "https://example.com", "expires_at": expiration})
    assert response.status_code == 422


def test_exact_expiration_boundary():
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert url_service.is_expired(URL(expires_at=now), now=now)
    assert not url_service.is_expired(URL(expires_at=now + timedelta(microseconds=1)), now=now)
    assert not url_service.is_expired(URL(expires_at=None), now=now)


def test_expiration_equal_to_now_is_rejected(client, monkeypatch):
    from app.schemas import url as schema
    fixed = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(schema, "utc_now", lambda: fixed)
    result = client.post("/api/v1/urls", json={"original_url": "https://example.com", "expires_at": fixed.isoformat()})
    assert result.status_code == 422


def test_existing_routes_are_not_shadowed(client):
    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
