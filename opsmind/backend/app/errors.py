"""Typed application errors and their HTTP mapping.

Every failure raises a typed ``AppError`` with the correct status code; nothing is
silently swallowed. A single handler renders them in one consistent problem shape.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("app.errors")

LLM_UNAVAILABLE_MESSAGE = "The assistant is briefly unavailable. Please try again in a moment."
DATABASE_UNAVAILABLE_MESSAGE = (
    "The service cannot reach its database right now. Please try again in a moment."
)


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    # The LLM chain fails loudly with the raw provider error (already logged upstream in
    # the failover chain) — a participant must never see that. Catch every LLMError that
    # reaches the HTTP boundary (all tiers down, rate-limited, malformed) and answer with
    # one calm 503. Registered on the LLMError base, so its subclasses resolve here too:
    # Starlette walks the exception's MRO. Imported locally because app.llm.base imports
    # this module, so a top-level import would be circular.
    from app.llm.base import LLMError

    @app.exception_handler(LLMError)
    async def _handle_llm_unavailable(_: Request, exc: LLMError) -> JSONResponse:
        logger.warning("LLM unavailable; returning 503 to client: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "llm_unavailable", "message": LLM_UNAVAILABLE_MESSAGE}},
        )

    # A database that has gone away gives the same treatment. SQLAlchemy does *not* wrap
    # a failure to connect — asyncpg's socket error propagates as a plain OSError — so
    # catching OperationalError would miss the case that actually happens when Postgres
    # stops: every endpoint answered with a bare, unexplained 500 while /health still
    # said "ok". The net is deliberately wide because that is the shape the failure
    # arrives in; nothing is swallowed, since the real exception is logged at error level.
    @app.exception_handler(OSError)
    async def _handle_database_unavailable(_: Request, exc: OSError) -> JSONResponse:
        logger.error("database unreachable; returning 503 to client: %r", exc)
        return JSONResponse(
            status_code=503,
            content={
                "error": {"code": "database_unavailable", "message": DATABASE_UNAVAILABLE_MESSAGE}
            },
        )
