from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.url import URL
from app.core.aliases import is_reserved_alias
from app.utils.short_code import generate_short_code

MAX_CODE_ATTEMPTS = 5
SHORT_CODE_INDEX = "ix_urls_short_code"


class ShortCodeGenerationError(Exception):
    """All attempts to reserve a unique short code failed."""


class AliasConflictError(Exception):
    """The requested alias is already in use."""


def create_short_url(
    db: Session, original_url: str, custom_alias: str | None = None,
    expires_at: datetime | None = None,
) -> URL:
    for _ in range(MAX_CODE_ATTEMPTS):
        code = custom_alias if custom_alias is not None else generate_short_code()
        if custom_alias is None and is_reserved_alias(code):
            continue
        record = URL(original_url=original_url, short_code=code, expires_at=expires_at)
        db.add(record)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            # Psycopg exposes the SQLSTATE and PostgreSQL constraint/index name.
            diag = getattr(exc.orig, "diag", None)
            if (getattr(exc.orig, "sqlstate", None) != "23505"
                    or getattr(diag, "constraint_name", None) != SHORT_CODE_INDEX):
                raise
            if custom_alias is not None:
                raise AliasConflictError("This alias is already in use") from exc
            continue
        db.refresh(record)
        return record
    raise ShortCodeGenerationError("Could not generate a unique short code")


def get_url_by_code(db: Session, short_code: str) -> URL | None:
    return db.scalar(select(URL).where(URL.short_code == short_code))


def expiration_in_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    # PostgreSQL returns aware datetimes. SQLite, used in unit tests, drops tzinfo.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_expired(record: URL, now: datetime | None = None) -> bool:
    expiration = expiration_in_utc(record.expires_at)
    return expiration is not None and expiration <= (now or datetime.now(timezone.utc))
