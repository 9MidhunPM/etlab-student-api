"""Explicit opt-in smoke against authorized credentials, without private output."""

import os
import re

import httpx
import pytest
from pydantic import ValidationError

from etlab_api.app import create_app
from etlab_api.models import RESOURCES, AcademicResponse, Capabilities
from etlab_api.settings import Settings

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("ETLAB_LIVE_TESTS") != "1",
        reason="Set ETLAB_LIVE_TESTS=1 to explicitly enable authorized upstream reads",
    ),
]

DEFAULT_CORE = (
    "profile",
    "dashboard",
    "semesters",
    "subjects",
    "timetable",
    "attendance",
    "results",
    "assessments",
)


def require(condition: bool, message: str) -> None:
    """Avoid pytest's assert introspection displaying private response values."""
    if not condition:
        pytest.fail(message, pytrace=False)


def safe_error_code(response: httpx.Response) -> str:
    """Expose only a short machine code, never a body or student field value."""
    try:
        payload = response.json()
    except ValueError:
        return "invalid_response"
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return "unknown_error"
    code = payload["error"].get("code")
    if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
        return "unknown_error"
    return code


async def test_live_student_read_smoke() -> None:
    """One login; validate discovered reads, metadata, cache, and cleanup only."""
    settings = Settings(app_mode="personal")
    token = settings.api_token.get_secret_value()
    requested = tuple(
        part.strip()
        for part in os.environ.get("ETLAB_LIVE_RESOURCES", ",".join(DEFAULT_CORE)).split(",")
        if part.strip()
    )
    require(bool(requested), "ETLAB_LIVE_RESOURCES must contain at least one resource")
    require(all(name in RESOURCES for name in requested), "Unknown live-test resource name")
    headers = {"Authorization": f"Bearer {token}"}

    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            health = await client.get("/healthz")
            require(health.status_code == 200, "Live application liveness check failed")
            unauthorized = await client.get("/v1/capabilities")
            require(unauthorized.status_code == 401, "Unauthenticated data was not rejected")

            response = await client.get("/v1/capabilities", headers=headers)
            require(
                response.status_code == 200,
                f"Live login/capability read failed: HTTP {response.status_code}, "
                f"code={safe_error_code(response)}",
            )
            try:
                capabilities = Capabilities.model_validate_json(response.content)
            except ValidationError:
                pytest.fail(
                    "Live capability response does not match the public schema", pytrace=False
                )
            available = {feature.resource for feature in capabilities.features if feature.available}
            selected = [resource for resource in requested if resource in available]
            require(
                bool(selected), "No requested core resource was discovered for the live account"
            )

            for resource in selected:
                response = await client.get(f"/v1/me/{resource}", headers=headers)
                require(response.status_code == 200, f"Live read failed for {resource}")
                require(
                    response.headers.get("cache-control") == "no-store", "Missing privacy header"
                )
                require(bool(response.headers.get("x-request-id")), "Missing request ID")
                try:
                    academic = AcademicResponse.model_validate_json(response.content)
                except ValidationError:
                    pytest.fail(f"Live response schema failed for {resource}", pytrace=False)
                require(bool(academic.data.title), f"Missing page heading for {resource}")
                require(academic.meta.total_rows >= 0, f"Invalid row-count metadata for {resource}")

                if settings.cache_ttl_seconds > 1:
                    cached_response = await client.get(f"/v1/me/{resource}", headers=headers)
                    require(
                        cached_response.status_code == 200,
                        f"Cached live read failed for {resource}",
                    )
                    try:
                        cached = AcademicResponse.model_validate_json(cached_response.content)
                    except ValidationError:
                        pytest.fail(f"Cached response schema failed for {resource}", pytrace=False)
                    if cached.meta.cache_hit:
                        require(
                            cached.meta.fetched_at == academic.meta.fetched_at,
                            f"Cache changed upstream fetch timestamp for {resource}",
                        )
                    else:
                        # Large responses may deliberately bypass the serialized
                        # cache budget, so a live cache miss is not itself a failure.
                        require(
                            cached.meta.fetched_at >= academic.meta.fetched_at,
                            f"Fresh fetch timestamp moved backwards for {resource}",
                        )

            inspection = await client.get("/v1/session", headers=headers)
            require(inspection.status_code == 200, "Live session inspection failed")
            deletion = await client.delete("/v1/session", headers=headers)
            require(deletion.status_code == 204, "Live session cleanup failed")
