import re
from datetime import datetime, timezone

from pydantic import AwareDatetime, BaseModel, Field, HttpUrl, field_validator

from app.core.aliases import is_reserved_alias


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class URLCreate(BaseModel):
    original_url: HttpUrl
    custom_alias: str | None = Field(default=None, min_length=3, max_length=30)
    expires_at: AwareDatetime | None = None

    @field_validator("original_url")
    @classmethod
    def url_fits_database(cls, value: HttpUrl) -> HttpUrl:
        if len(str(value)) > 2048:
            raise ValueError("URL must be at most 2048 characters after normalization")
        return value

    @field_validator("custom_alias")
    @classmethod
    def validate_custom_alias(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*[A-Za-z0-9]", value):
            raise ValueError("Alias must contain only letters, digits, hyphens, or underscores and start and end with a letter or digit")
        if is_reserved_alias(value):
            raise ValueError("This alias is reserved")
        return value

    @field_validator("expires_at", mode="before")
    @classmethod
    def require_iso_timestamp(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str) or not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", value, re.IGNORECASE):
            raise ValueError("Expiration must be an ISO 8601 timestamp with a timezone")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expiration(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        value = value.astimezone(timezone.utc)
        if value <= utc_now():
            raise ValueError("Expiration must be in the future")
        return value


class URLResponse(BaseModel):
    id: int
    original_url: HttpUrl
    short_code: str
    short_url: HttpUrl
    expires_at: AwareDatetime | None
