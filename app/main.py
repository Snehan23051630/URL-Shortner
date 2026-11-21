from fastapi import FastAPI

from app.api.routes.health import router as health_router
from app.api.routes.urls import router as urls_router

app = FastAPI(title="URL Shortener")
app.include_router(health_router)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "URL Shortener API"}


# Keep the catch-all short-code route after the root and health routes.
app.include_router(urls_router)
