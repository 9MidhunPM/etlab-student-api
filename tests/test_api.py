"""Exercise HTTP contracts, authorization boundaries, and secret-safe failures."""

from contextlib import asynccontextmanager

import httpx
import pytest

from etlab_api.app import create_app
from etlab_api.errors import EtlabError
from etlab_api.models import RESOURCES, AcademicPage, Capabilities, Download, Table
from etlab_api.settings import Settings

pytestmark = pytest.mark.anyio
TOKEN = "deployment-token-" + "x" * 32


class FakeAdapter:
    def __init__(self, base_url):
        self.base_url = base_url
        self.username = ""
        self.logins = 0
        self.calls = []
        self.closed = False
        self.error = None
        self.file = Download(filename="grade.txt", content_type="text/plain", content=b"file")

    async def login(self, username, password):
        self.logins += 1
        self.username = username

    async def capabilities(self):
        return Capabilities(
            institution=self.base_url, adapter="fake", adapter_version="1", features=[]
        )

    async def read(self, resource, filters=None, detail_id=None):
        self.calls.append((resource, filters, detail_id))
        if self.error:
            raise self.error
        return AcademicPage(
            title=resource,
            fields={"Student": self.username},
            tables=[
                Table(columns=["Subject"], rows=[{"Subject": str(number)} for number in range(5)])
            ],
        )

    async def download(self, attachment_id):
        if self.error:
            raise self.error
        return self.file

    async def close(self):
        self.closed = True


def make_app(mode="personal", **overrides):
    settings = Settings(
        _env_file=None,
        app_mode=mode,
        api_token=TOKEN,
        etlab_username="personal-student",
        etlab_password="private-password",
        **overrides,
    )
    adapters = []

    def factory(base_url, _):
        adapter = FakeAdapter(base_url)
        adapters.append(adapter)
        return adapter

    return create_app(settings, factory), adapters


@asynccontextmanager
async def client_for(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


async def test_health_liveness_has_no_login_and_no_store():
    app, adapters = make_app()
    async with client_for(app) as client:
        response = await client.get("/healthz")
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-request-id"]
        assert adapters == []


async def test_authorization_and_error_envelope_have_request_ids():
    app, adapters = make_app()
    async with client_for(app) as client:
        for headers in ({}, auth("wrong"), {"Authorization": "Basic secret"}):
            response = await client.get("/v1/me/profile", headers=headers)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "unauthorized"
            assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
            assert response.headers["www-authenticate"] == "Bearer"
        assert adapters == []


async def test_academic_resource_filter_detail_and_cache_contract():
    app, adapters = make_app()
    async with client_for(app) as client:
        params = {"StudentAttendance[semester]": "3", "page": "2", "page_size": "2"}
        first = await client.get("/v1/me/attendance", params=params, headers=auth())
        assert first.status_code == 200
        body = first.json()
        assert body["data"]["tables"][0]["rows"] == [{"Subject": "2"}, {"Subject": "3"}]
        assert body["meta"]["total_rows"] == 5 and not body["meta"]["cache_hit"]
        assert adapters[0].calls == [("attendance", {"StudentAttendance[semester]": "3"}, None)]
        second = await client.get(
            "/v1/me/attendance", params={"StudentAttendance[semester]": "3"}, headers=auth()
        )
        assert len(second.json()["data"]["tables"][0]["rows"]) == 5
        assert second.json()["meta"]["cache_hit"]
        assert second.json()["meta"]["fetched_at"] == body["meta"]["fetched_at"]
        detail = await client.get("/v1/me/attendance/link-id", headers=auth())
        assert detail.status_code == 200
        assert adapters[0].calls[-1] == ("attendance", {}, "link-id")


async def test_invalid_queries_do_not_fetch_upstream():
    app, adapters = make_app()
    async with client_for(app) as client:
        for query in ("page=0", "page_size=501", "semester=3&semester=4", "page=nope"):
            response = await client.get(f"/v1/me/profile?{query}", headers=auth())
            assert response.status_code == 422
        assert all(adapter.logins == 0 and not adapter.calls for adapter in adapters)


async def test_all_resources_are_concrete_in_openapi_and_read_only():
    app, _ = make_app()
    schema = app.openapi()
    for resource in RESOURCES:
        assert f"/v1/me/{resource}" in schema["paths"]
        assert f"/v1/me/{resource}/{{detail_id}}" in schema["paths"]
        assert set(schema["paths"][f"/v1/me/{resource}"]) == {"get"}
        assert schema["paths"][f"/v1/me/{resource}"]["get"]["security"] == [{"BearerToken": []}]
    assert "{resource}" not in str(schema["paths"])
    assert "session_token" in schema["components"]["schemas"]["SessionCreated"]["properties"]


async def test_multiuser_deployment_token_admits_but_cannot_read_data():
    app, adapters = make_app("multiuser")
    async with client_for(app) as client:
        payload = {"base_url": "https://one.etlab.in", "username": "one", "password": "secret"}
        response = await client.post("/v1/sessions", json=payload, headers=auth())
        assert response.status_code == 201
        token = response.json()["session_token"]
        assert "secret" not in response.text and token != TOKEN
        denied = await client.get("/v1/me/profile", headers=auth())
        assert (
            denied.status_code == 403 and denied.json()["error"]["code"] == "session_token_required"
        )
        student = await client.get("/v1/me/profile", headers=auth(token))
        assert student.status_code == 200 and student.json()["data"]["fields"]["Student"] == "one"
        no_admission = await client.post("/v1/sessions", json=payload, headers=auth(token))
        assert no_admission.status_code == 401
        info = await client.get("/v1/session", headers=auth(token))
        assert info.status_code == 200 and info.json()["mode"] == "multiuser"
        assert (await client.delete("/v1/session", headers=auth(token))).status_code == 204
        assert (await client.get("/v1/me/profile", headers=auth(token))).status_code == 401
        assert adapters[0].closed


async def test_personal_mode_rejects_multiuser_admission():
    app, _ = make_app()
    async with client_for(app) as client:
        response = await client.post(
            "/v1/sessions",
            headers=auth(),
            json={
                "base_url": "https://one.etlab.in",
                "username": "one",
                "password": "secret",
            },
        )
        assert response.status_code == 409 and response.json()["error"]["code"] == "mode_mismatch"


async def test_validation_failure_never_echoes_credentials():
    app, _ = make_app("multiuser")
    async with client_for(app) as client:
        secret = "this-password-must-never-be-echoed"
        response = await client.post(
            "/v1/sessions",
            headers=auth(),
            json={
                "base_url": "https://one.etlab.in",
                "username": {"private-user": "value"},
                "password": secret,
            },
        )
        assert response.status_code == 422
        assert secret not in response.text and "private-user" not in response.text
        assert set(response.json()) == {"error"}
        assert "detail" not in response.json()


async def test_safe_unexpected_errors_and_upstream_error_distinction(caplog):
    app, adapters = make_app()
    async with client_for(app) as client:
        await client.get("/v1/session", headers=auth())
        adapters[0].error = EtlabError("unsupported_feature", "The module is unavailable.", 404)
        response = await client.get("/v1/me/library", headers=auth())
        assert (
            response.status_code == 404
            and response.json()["error"]["code"] == "unsupported_feature"
        )
        adapters[0].error = RuntimeError("password-and-cookie-contents")
        response = await client.get("/v1/me/library", headers=auth())
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert "password-and-cookie-contents" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert "password-and-cookie-contents" not in caplog.text
        # Default ASGITransport re-raises server exceptions, so this also proves
        # the app contains failures before Uvicorn could log their raw payload.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as strict_client:
            strict = await strict_client.get("/v1/me/library", headers=auth())
            assert strict.status_code == 500


async def test_attachment_download_sanitizes_headers_and_enforces_limit():
    app, adapters = make_app(max_download_bytes=5)
    async with client_for(app) as client:
        await client.get("/v1/session", headers=auth())
        adapters[0].file = Download(
            filename='../../grades\r\nX-Inject: yes " résumé.pdf',
            content_type="application/pdf\r\nX-Inject: yes",
            content=b"file",
        )
        response = await client.get("/v1/attachments/file-id", headers=auth())
        assert response.status_code == 200 and response.content == b"file"
        assert response.headers["content-type"] == "application/octet-stream"
        assert "\r" not in response.headers["content-disposition"]
        assert "../" not in response.headers["content-disposition"]
        assert "filename*=UTF-8''" in response.headers["content-disposition"]
        assert "X-Inject" not in response.headers
        adapters[0].file = Download(
            filename="large.pdf", content_type="application/pdf", content=b"123456"
        )
        response = await client.get("/v1/attachments/large-file", headers=auth())
        assert response.status_code == 413


async def test_docs_toggle_and_cors_default_and_configured_origin():
    app, _ = make_app(docs_enabled=False)
    async with client_for(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404
        default = await client.get("/healthz", headers={"Origin": "https://front.example"})
        assert "access-control-allow-origin" not in default.headers
    enabled, _ = make_app(cors_origins=("https://front.example",))
    async with client_for(enabled) as client:
        response = await client.options(
            "/v1/me/profile",
            headers={
                "Origin": "https://front.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://front.example"


async def test_login_rate_limit_http_status_and_retry_after():
    app, _ = make_app("multiuser", login_attempts_per_minute=1)
    async with client_for(app) as client:
        payload = {"base_url": "https://one.etlab.in", "username": "one", "password": "secret"}
        assert (await client.post("/v1/sessions", json=payload, headers=auth())).status_code == 201
        limited = await client.post("/v1/sessions", json=payload, headers=auth())
        assert limited.status_code == 429 and limited.headers["retry-after"] == "60"
