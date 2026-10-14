"""The ``app`` service's ASGI application.

Run locally with ``uvicorn --factory collicity_app.main:create_app``.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from collicity_app.settings import AppSettings
from collicity_db import make_engine

# API responses are data, never documents: nothing in them may load, frame or be cached.
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings()  # pyright: ignore[reportCallIssue]
    engine = make_engine(
        settings.database_url.get_secret_value(), connect_args={"connect_timeout": 3}
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="Collicity API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.environment == "local" else None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def security_headers(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path == app.docs_url:
            # Swagger UI loads scripts and styles; it is served only in local development.
            del response.headers["Content-Security-Policy"]
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness: the process is serving requests."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Readiness: the database answers as app_user."""
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    return app
