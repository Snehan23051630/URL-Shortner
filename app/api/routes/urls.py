from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import get_base_url
from app.db.session import get_db
from app.schemas.url import URLCreate, URLResponse
from app.services import url_service

router = APIRouter(tags=["urls"])
DbSession = Annotated[Session, Depends(get_db)]


@router.post("/api/v1/urls", response_model=URLResponse, status_code=status.HTTP_201_CREATED)
def create_url(payload: URLCreate, db: DbSession) -> URLResponse:
    base_url = get_base_url()
    try:
        record = url_service.create_short_url(
            db, str(payload.original_url), payload.custom_alias, payload.expires_at
        )
    except url_service.AliasConflictError as exc:
        raise HTTPException(status_code=409, detail="Alias already in use") from exc
    except url_service.ShortCodeGenerationError as exc:
        raise HTTPException(status_code=503, detail="Could not generate a unique short code") from exc
    return URLResponse(
        id=record.id,
        original_url=record.original_url,
        short_code=record.short_code,
        short_url=f"{base_url}/{record.short_code}",
        expires_at=url_service.expiration_in_utc(record.expires_at),
    )


@router.get("/{short_code}", response_class=RedirectResponse, status_code=307)
def redirect_url(short_code: str, db: DbSession) -> RedirectResponse:
    record = url_service.get_url_by_code(db, short_code)
    if record is None:
        raise HTTPException(status_code=404, detail="Short URL not found")
    if url_service.is_expired(record):
        raise HTTPException(status_code=410, detail="Short URL has expired")
    return RedirectResponse(url=record.original_url, status_code=307)
