"""Opt-in tests in a temporary PostgreSQL schema, never the application's tables."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# The application engine is not used by these tests; the dependency is overridden.
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")

from app.db.session import get_db
from app.main import app
from app.models.url import URL
from app.services import url_service


@pytest.fixture
def postgres_engine():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to run isolated PostgreSQL integration tests")
    schema = "url_test_" + uuid4().hex
    admin = create_engine(url)
    engine = None
    created = False
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        created = True
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0001_create_urls.py"
        spec = importlib.util.spec_from_file_location("initial_url_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def test_concurrent_alias_and_utc_storage(postgres_engine, monkeypatch):
    factory = sessionmaker(bind=postgres_engine, expire_on_commit=False)
    barrier = Barrier(2, timeout=10)

    def override_db():
        with factory() as db:
            barrier.wait()
            yield db

    app.dependency_overrides[get_db] = override_db
    future = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(timezone(timedelta(hours=5, minutes=30)))
    payload = {"original_url": "https://example.com", "custom_alias": "same-alias", "expires_at": future.isoformat()}

    def create():
        with TestClient(app, raise_server_exceptions=False) as client:
            return client.post("/api/v1/urls", json=payload)

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(lambda _: create(), range(2)))
        assert sorted(response.status_code for response in responses) == [201, 409]
        with Session(postgres_engine) as db:
            records = db.query(URL).filter_by(short_code="same-alias").all()
            assert len(records) == 1
            assert records[0].expires_at.utcoffset() is not None
            assert records[0].expires_at.astimezone(timezone.utc) == future

        def ordinary_db():
            with factory() as db:
                yield db

        app.dependency_overrides[get_db] = ordinary_db
        with TestClient(app, raise_server_exceptions=False) as client:
            codes = iter(["same-alias", "fresh12"])
            monkeypatch.setattr(url_service, "generate_short_code", lambda: next(codes))
            generated = client.post("/api/v1/urls", json={"original_url": "https://example.com"})
            assert generated.status_code == 201
            assert generated.json()["short_code"] == "fresh12"
            with Session(postgres_engine) as db:
                existing = db.query(URL).filter_by(short_code="same-alias").one()
                existing.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
                db.commit()
            assert client.get("/same-alias", follow_redirects=False).status_code == 410
            assert client.post("/api/v1/urls", json=payload).status_code == 409
    finally:
        app.dependency_overrides.clear()
