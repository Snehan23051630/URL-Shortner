"""Read the same local environment variables for the app and Alembic."""

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from sqlalchemy import URL

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@lru_cache
def get_base_url() -> str:
    value = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise ValueError("BASE_URL must be an absolute HTTP(S) URL without credentials, query, or fragment")
    try:
        parts.port  # Validate the port if supplied.
    except ValueError as exc:
        raise ValueError("BASE_URL has an invalid port") from exc
    return value


@lru_cache
def get_database_url() -> URL:
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ValueError(f"Missing database settings: {', '.join(missing)}")
    return URL.create(
        "postgresql+psycopg",
        username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.environ["POSTGRES_DB"],
    )
