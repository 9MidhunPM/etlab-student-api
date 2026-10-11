"""Bearer-protected REST API with a concrete, documented route for each module."""

import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Query, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException

from etlab_api.errors import EtlabError
from etlab_api.models import (
    RESOURCES,
    AcademicResponse,
    Capabilities,
    ErrorResponse,
    ResponseMeta,
    SessionCreate,
    SessionCreated,
    SessionInfo,
)
from etlab_api.sessions import Adapter, AdapterFactory, ManagedSession, SessionService
from etlab_api.settings import Settings


def _default_adapter_factory(base_url: str, settings: Settings) -> Adapter:
    from etlab_api.adapters.student import StudentAdapter
    from etlab_api.upstream import UpstreamClient

    return StudentAdapter(UpstreamClient(base_url, settings))


def _filters(request: Request) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key in {"page", "page_size"}:
            continue
        if key in result:
            raise EtlabError("invalid_filters", "Duplicate filter names are not supported.", 422)
        if not key or len(key) > 255 or len(value) > 1024 or len(result) >= 32:
            raise EtlabError(
                "invalid_filters", "Filter names or values exceed allowed limits.", 422
            )
        result[key] = value
    return result


def _download_disposition(filename: str) -> str:
    # Treat the upstream filename as data, never as a filesystem path or header.
    clean = filename.replace("\\", "/").rsplit("/", 1)[-1]
    clean = "".join(char for char in clean if ord(char) >= 32 and ord(char) != 127)[:200]
    clean = clean or "attachment"
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", clean).strip(" .") or "attachment"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(clean, safe='')}"


def create_app(
    settings: Settings | None = None,
    adapter_factory: AdapterFactory | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """Build lazily for ``uvicorn etlab_api.app:create_app --factory``."""
    # BaseSettings supplies required values from the environment at runtime.
    configuration = settings if settings is not None else Settings()
    service = SessionService(configuration, adapter_factory or _default_adapter_factory, clock)

    async def sweep() -> None:
        while True:
            await asyncio.sleep(min(30, configuration.session_idle_seconds))
            await service.cleanup()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await service.shutdown()

    app = FastAPI(
        title="ETLab Student API",
        description=(
            "Self-hosted, read-only access to ETLab student modules. Authenticate with the "
            "deployment bearer token in personal mode. In multiuser mode create a session "
            "with that token, then use the returned student session token for academic data. "
            "Resource responses preserve institutional labels. Additional query parameters "
            "must use exact names advertised by the response's filters collection."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if configuration.docs_enabled else None,
        redoc_url="/redoc" if configuration.docs_enabled else None,
        openapi_url="/openapi.json" if configuration.docs_enabled else None,
        swagger_ui_parameters={"persistAuthorization": False},
        responses={
            401: {
                "model": ErrorResponse,
                "description": "Missing, invalid, or expired bearer token",
            },
            403: {"model": ErrorResponse, "description": "Insufficient permission"},
            422: {"model": ErrorResponse, "description": "Invalid request or upstream filter"},
            502: {"model": ErrorResponse, "description": "Unexpected upstream content"},
            503: {"model": ErrorResponse, "description": "Temporary upstream or capacity failure"},
        },
    )
    app.state.session_service = service
    if configuration.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(configuration.cors_origins),
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
            expose_headers=["X-Request-ID"],
        )

    @app.middleware("http")
    async def response_headers(request: Request, call_next: Any) -> Response:
        request.state.request_id = uuid.uuid4().hex
        try:
            response: Response = await call_next(request)
        except Exception:
            # Contain the exception here: an outer ServerErrorMiddleware handler
            # re-raises it and Uvicorn may log credential-bearing exception text.
            response = error_response(request, 500, "internal_error", "An internal error occurred.")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        headers = {"Cache-Control": "no-store", "X-Request-ID": request_id}
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if status == 429:
            headers["Retry-After"] = "60"
        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": message, "request_id": request_id}},
            headers=headers,
        )

    @app.exception_handler(EtlabError)
    async def etlab_error(request: Request, error: EtlabError) -> JSONResponse:
        return error_response(request, error.status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _: RequestValidationError) -> JSONResponse:
        # FastAPI's default errors include submitted input, including passwords.
        return error_response(request, 422, "invalid_request", "Request validation failed.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return error_response(
            request, error.status_code, "http_error", "The request is not supported."
        )

    bearer = HTTPBearer(auto_error=False, scheme_name="BearerToken")

    async def bearer_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
    ) -> str:
        if credentials is None:
            raise EtlabError("unauthorized", "A valid bearer token is required.", 401)
        return credentials.credentials

    async def deployment_token(token: Annotated[str, Depends(bearer_token)]) -> None:
        if not service.is_deployment_token(token):
            raise EtlabError("unauthorized", "The deployment bearer token is required.", 401)

    async def student_session(token: Annotated[str, Depends(bearer_token)]) -> ManagedSession:
        return await service.authenticate(token)

    @app.get("/healthz", tags=["Operations"], summary="Worker liveness")
    async def health() -> dict[str, str]:
        """Checks the worker only; does not authenticate or contact ETLab."""
        return {"status": "ok"}

    @app.post(
        "/v1/sessions",
        response_model=SessionCreated,
        status_code=201,
        dependencies=[Depends(deployment_token)],
        tags=["Sessions"],
        responses={429: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def create_session(body: SessionCreate, request: Request) -> SessionCreated:
        """Log in in multiuser mode. Keep the returned token private; passwords are discarded."""
        caller = request.client.host if request.client else "unknown"
        return await service.create(
            body.base_url, body.username, body.password.get_secret_value(), caller
        )

    @app.get("/v1/session", response_model=SessionInfo, tags=["Sessions"])
    async def inspect_session(
        session: Annotated[ManagedSession, Depends(student_session)],
    ) -> SessionInfo:
        """Inspect this session and its current effective expiry."""
        return service.info(session)

    @app.delete("/v1/session", status_code=204, tags=["Sessions"])
    async def delete_session(
        session: Annotated[ManagedSession, Depends(student_session)],
    ) -> Response:
        """Discard cookies and cached academic data. Personal mode logs in again on the next read."""
        await service.delete(session)
        return Response(status_code=204)

    @app.get("/v1/capabilities", response_model=Capabilities, tags=["Academic data"])
    async def capabilities(
        session: Annotated[ManagedSession, Depends(student_session)],
    ) -> Capabilities:
        """Discover module availability for this account; availability is not live verification."""
        return await service.capabilities(session)

    async def academic_response(
        request: Request,
        session: ManagedSession,
        resource: str,
        page: int,
        page_size: int,
        detail_id: str | None = None,
    ) -> AcademicResponse:
        data, fetched_at, cache_hit = await service.read(
            session, resource, _filters(request), detail_id
        )
        total_rows = sum(len(table.rows) for table in data.tables)
        start = (page - 1) * page_size
        # Paginate every table independently; retain fields, labels, and page text.
        for table in data.tables:
            table.rows = table.rows[start : start + page_size]
        return AcademicResponse(
            data=data,
            meta=ResponseMeta(
                institution=session.institution,
                fetched_at=datetime.fromtimestamp(fetched_at, UTC),
                cache_hit=cache_hit,
                page=page,
                page_size=page_size,
                total_rows=total_rows,
            ),
        )

    def resource_endpoint(resource: str) -> Callable[..., Any]:
        async def endpoint(
            request: Request,
            session: Annotated[ManagedSession, Depends(student_session)],
            page: Annotated[int, Query(ge=1, description="Local page, applied to each table")] = 1,
            page_size: Annotated[int, Query(ge=1, le=500)] = 100,
        ) -> AcademicResponse:
            return await academic_response(request, session, resource, page, page_size)

        return endpoint

    def detail_endpoint(resource: str) -> Callable[..., Any]:
        async def endpoint(
            detail_id: str,
            request: Request,
            session: Annotated[ManagedSession, Depends(student_session)],
            page: Annotated[int, Query(ge=1)] = 1,
            page_size: Annotated[int, Query(ge=1, le=500)] = 100,
        ) -> AcademicResponse:
            return await academic_response(request, session, resource, page, page_size, detail_id)

        return endpoint

    for resource in RESOURCES:
        app.add_api_route(
            f"/v1/me/{resource}",
            resource_endpoint(resource),
            methods=["GET"],
            response_model=AcademicResponse,
            tags=["Academic data"],
            operation_id=f"read_{resource}",
            summary=f"Read {resource}",
            description=(
                "Additional query parameters use exact discovered filter names (including brackets). "
                "Fetch once to inspect data.filters; unsupported filters return an explicit error. "
                "Pagination is local and applies to each table; total_rows sums all unpaginated tables. "
                "fetched_at retains the original upstream fetch timestamp on cache hits."
            ),
        )
        app.add_api_route(
            f"/v1/me/{resource}/{{detail_id}}",
            detail_endpoint(resource),
            methods=["GET"],
            response_model=AcademicResponse,
            tags=["Academic data"],
            operation_id=f"read_{resource}_detail",
            summary=f"Read {resource} detail",
            description="Use an opaque detail link ID returned by this session's resource response.",
        )

    @app.get(
        "/v1/attachments/{attachment_id}",
        response_class=Response,
        tags=["Attachments"],
        responses={
            200: {"description": "Download bytes", "content": {"application/octet-stream": {}}}
        },
    )
    async def download(
        attachment_id: str,
        session: Annotated[ManagedSession, Depends(student_session)],
    ) -> Response:
        """Download a session-owned attachment ID with configured size and origin limits."""
        result = await service.download(session, attachment_id)
        if len(result.content) > configuration.max_download_bytes:
            raise EtlabError(
                "response_too_large", "Attachment exceeds the configured size limit.", 413
            )
        media_type = result.content_type.split(";", 1)[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media_type):
            media_type = "application/octet-stream"
        return Response(
            content=result.content,
            media_type=media_type,
            headers={"Content-Disposition": _download_disposition(result.filename)},
        )

    return app
