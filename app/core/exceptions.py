"""Domain exceptions + FastAPI handlers.

Keeping a small exception hierarchy lets the API layer translate failures into
clean HTTP responses without leaking internals (e.g. raw SQL or DB errors) to
the client.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse


class ReportingError(Exception):
    """Base class for all application-level errors."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    message: str = "Internal error"

    def __init__(self, message: "str | None" = None) -> None:
        self.message = message or self.message
        super().__init__(self.message)


class SQLGenerationError(ReportingError):
    """The LLM failed to produce usable SQL."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "Could not translate the question into a query."


class UnsafeSQLError(ReportingError):
    """Generated SQL violated the read-only / safety policy."""

    status_code = status.HTTP_400_BAD_REQUEST
    message = "The generated query was rejected for safety reasons."


class QueryExecutionError(ReportingError):
    """The (validated) SQL failed to execute against the database."""

    status_code = status.HTTP_400_BAD_REQUEST
    message = "The query could not be executed."


class IngestionError(ReportingError):
    """Excel -> Postgres pipeline failure."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "The source file could not be ingested."


class LLMServiceError(ReportingError):
    """Upstream Anthropic API failure / timeout."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "The analysis service is temporarily unavailable."


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ReportingError)
    async def _handle(request: Request, exc: ReportingError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.__class__.__name__, "detail": exc.message},
        )
