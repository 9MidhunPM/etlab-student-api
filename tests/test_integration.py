"""Real API/session/adapter/transport seams against a synthetic cookie-backed portal."""

from collections import Counter
from contextlib import asynccontextmanager
from html import escape
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

import httpx
import pytest

from etlab_api.adapters.student import StudentAdapter
from etlab_api.app import create_app
from etlab_api.settings import Settings
from etlab_api.upstream import UpstreamClient

pytestmark = pytest.mark.anyio
DEPLOYMENT_TOKEN = "synthetic-deployment-token-" + "x" * 32
BASE = "https://one.etlab.in"
LOGIN = """<html><body><main><h1>Student login</h1>
<form method="post" action="/user/login">
<input type="hidden" name="YII_CSRF_TOKEN" value="synthetic-csrf">
<input name="LoginForm[username]"><input type="password" name="LoginForm[password]">
</form></main></body></html>"""


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


class Portal:
    """Models login redirects, independent cookie jars, reads, and expiration."""

    def __init__(self):
        self.sessions = {}
        self.logins = 0
        self.requests = []
        self.counts = Counter()
        self.attended = "20"
        self.unsafe_navigation = False
        self.redirects = {}
        self.login_redirect = None
        self.mutations = 0

    def expire_all(self):
        self.sessions.clear()

    def dashboard(self):
        attendance = (
            "/student/attendance?StudentAttendance[action]=delete"
            if self.unsafe_navigation
            else "/student/attendance"
        )
        return f"""<html><body><nav>
        <a href="/student/profile">Profile</a><a href="{attendance}">Attendance</a>
        <a href="/student/studymaterials">Materials</a><a href="/user/logout">Logout</a>
        </nav><main><h1>Student dashboard</h1><p>Welcome.</p></main></body></html>"""

    def __call__(self, request):
        host, path = request.url.host, request.url.path
        self.requests.append((request.method, host, path, str(request.url.query)))
        self.counts[(host, path)] += 1
        if request.method == "POST" and path == "/user/login":
            if self.login_redirect is not None:
                return httpx.Response(307, headers={"Location": self.login_redirect})
            data = parse_qs(request.content.decode())
            assert data["YII_CSRF_TOKEN"] == ["synthetic-csrf"]
            username = data["LoginForm[username]"][0]
            assert data["LoginForm[password]"] == ["synthetic-password"]
            self.logins += 1
            token = f"synthetic-cookie-{self.logins}"
            self.sessions[(host, token)] = username
            return httpx.Response(
                303,
                headers={
                    "Location": "/student/index",
                    "Set-Cookie": f"session={token}; Secure; HttpOnly; Path=/",
                },
            )
        jar = SimpleCookie()
        jar.load(request.headers.get("cookie", ""))
        token = jar["session"].value if "session" in jar else ""
        username = self.sessions.get((host, token))
        if path == "/user/login" or (path == "/" and username is None):
            return self.html(LOGIN)
        if username is None:
            return httpx.Response(302, headers={"Location": "/user/login"})
        if path in self.redirects:
            return httpx.Response(302, headers={"Location": self.redirects[path]})
        if path == "/student/delete":
            self.mutations += 1
            return self.html("<main><h1>Deleted</h1><p>Record deleted.</p></main>")
        if path in {"/", "/student/index"}:
            return self.html(self.dashboard())
        if path == "/student/profile":
            return self.html(
                "<main><h1>Student profile</h1><dl><dt>Student</dt><dd>"
                + escape(username)
                + "</dd><dt>College</dt><dd>"
                + escape(host)
                + "</dd></dl></main>"
            )
        if path == "/student/attendance":
            return self.html(f"""<main><h1>Attendance</h1>
            <table><tr><th>Subject</th><th>Attended</th></tr>
            <tr><td>CS201</td><td>{self.attended}</td></tr></table>
            <a href="/student/viewattendancesubject/101">Class details</a>
            </main>""")
        if path == "/student/viewattendancesubject/101":
            return self.html(
                "<main><h1>Class details</h1><p>Student: " + escape(username) + "</p></main>"
            )
        if path == "/student/studymaterials":
            return self.html("""<main><h1>Materials</h1><p>Available notes.</p>
            <a href="/uploads/notes.pdf">Lecture notes</a></main>""")
        if path == "/uploads/notes.pdf":
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Disposition": 'attachment; filename="notes.pdf"',
                },
                content=b"%PDF synthetic notes for " + username.encode(),
            )
        return httpx.Response(404, content=b"Unknown synthetic route")

    @staticmethod
    def html(value):
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=value)


@asynccontextmanager
async def integration_client(mode="personal", portal=None, clock=None):
    portal = portal or Portal()
    settings = Settings(
        _env_file=None,
        app_mode=mode,
        api_token=DEPLOYMENT_TOKEN,
        etlab_base_url=BASE,
        etlab_username="synthetic-personal-student",
        etlab_password="synthetic-password",
        upstream_interval_seconds=0,
    )
    adapters = []

    def factory(base_url, configuration):
        adapter = StudentAdapter(
            UpstreamClient(base_url, configuration, httpx.MockTransport(portal))
        )
        adapters.append(adapter)
        return adapter

    app = create_app(settings, factory, clock or Clock())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api.test"
        ) as client:
            yield client, portal, adapters


def auth(token=DEPLOYMENT_TOKEN):
    return {"Authorization": f"Bearer {token}"}


async def new_student_session(client, username, base_url=BASE):
    response = await client.post(
        "/v1/sessions",
        headers=auth(),
        json={
            "base_url": base_url,
            "username": username,
            "password": "synthetic-password",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["session_token"]


async def test_personal_detail_expiry_reauthenticates_once_and_preserves_opaque_id():
    async with integration_client() as (client, portal, _):
        resource = await client.get("/v1/me/attendance", headers=auth())
        assert resource.status_code == 200
        link = resource.json()["data"]["links"][0]
        assert link["kind"] == "detail" and "101" not in link["id"]
        portal.expire_all()
        portal.attended = "21"
        detail = await client.get(f"/v1/me/attendance/{link['id']}", headers=auth())
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["text"] == ["Student: synthetic-personal-student"]
        assert portal.logins == 2
        assert portal.counts[("one.etlab.in", "/student/viewattendancesubject/101")] == 2
        # The returned ID remains usable; response caching does not expose a stale login page.
        repeated = await client.get(f"/v1/me/attendance/{link['id']}", headers=auth())
        assert repeated.status_code == 200 and repeated.json()["meta"]["cache_hit"]
        assert portal.logins == 2
        fresh = await client.get("/v1/me/attendance", headers=auth())
        assert not fresh.json()["meta"]["cache_hit"]
        assert fresh.json()["data"]["tables"][0]["rows"][0]["Attended"] == "21"


async def test_personal_attachment_expiry_reauthenticates_once_and_preserves_opaque_id():
    async with integration_client() as (client, portal, _):
        resource = await client.get("/v1/me/materials", headers=auth())
        assert resource.status_code == 200
        link = resource.json()["data"]["links"][0]
        assert link["kind"] == "attachment"
        portal.expire_all()
        download = await client.get(f"/v1/attachments/{link['id']}", headers=auth())
        assert download.status_code == 200, download.text
        assert download.content == b"%PDF synthetic notes for synthetic-personal-student"
        assert download.headers["content-type"] == "application/pdf"
        assert "attachment" in download.headers["content-disposition"]
        assert portal.logins == 2
        assert portal.counts[("one.etlab.in", "/uploads/notes.pdf")] == 2


@pytest.mark.parametrize(
    "location",
    [
        "/student/delete?id=42",
        "/student/attendance?StudentAttendance[action]=delete",
    ],
)
async def test_reviewed_read_cannot_redirect_to_a_mutating_action_or_nested_control(location):
    portal = Portal()
    portal.redirects["/student/attendance"] = location
    async with integration_client(portal=portal) as (client, portal, _):
        response = await client.get("/v1/me/attendance", headers=auth())
        assert response.status_code in {403, 405, 422}, response.text
        assert response.json()["error"]["code"] in {"operation_denied", "destination_denied"}
        assert portal.mutations == 0
        assert portal.counts[("one.etlab.in", "/student/attendance")] == 1
        assert not any(path == "/student/delete" for _, _, path, _ in portal.requests)


async def test_nested_action_link_is_not_discovered_as_a_module():
    portal = Portal()
    portal.unsafe_navigation = True
    async with integration_client(portal=portal) as (client, portal, _):
        response = await client.get("/v1/capabilities", headers=auth())
        assert response.status_code == 200
        attendance = next(
            feature
            for feature in response.json()["features"]
            if feature["resource"] == "attendance"
        )
        assert not attendance["available"] and attendance["status"] == "unavailable"
        unavailable = await client.get("/v1/me/attendance", headers=auth())
        assert unavailable.status_code == 404
        assert unavailable.json()["error"]["code"] == "unsupported_resource"
        assert portal.counts[("one.etlab.in", "/student/attendance")] == 0


async def test_login_redirect_cannot_forward_credentials_to_mutation_action():
    portal = Portal()
    portal.login_redirect = "/student/delete?id=42"
    async with integration_client(portal=portal) as (client, portal, _):
        response = await client.get("/v1/me/profile", headers=auth())
        assert response.status_code == 405
        assert response.json()["error"]["code"] == "operation_denied"
        assert portal.counts[("one.etlab.in", "/user/login")] == 1
        assert not any(path == "/student/delete" for _, _, path, _ in portal.requests)
        assert "synthetic-password" not in response.text


async def test_attachment_redirect_cannot_escape_to_mutation_action():
    portal = Portal()
    portal.redirects["/uploads/notes.pdf"] = "/student/delete?id=42"
    async with integration_client(portal=portal) as (client, portal, _):
        materials = await client.get("/v1/me/materials", headers=auth())
        attachment_id = materials.json()["data"]["links"][0]["id"]
        response = await client.get(f"/v1/attachments/{attachment_id}", headers=auth())
        assert response.status_code == 405
        assert response.json()["error"]["code"] == "operation_denied"
        assert portal.mutations == 0
        assert not any(path == "/student/delete" for _, _, path, _ in portal.requests)


async def test_multiuser_cookies_and_opaque_links_isolate_students_and_colleges():
    async with integration_client("multiuser") as (client, portal, _):
        alice = await new_student_session(client, "synthetic-alice")
        bob = await new_student_session(client, "synthetic-bob")
        other_college = await new_student_session(client, "synthetic-alice", "https://two.etlab.in")
        for token, expected_student, expected_college in (
            (alice, "synthetic-alice", "one.etlab.in"),
            (bob, "synthetic-bob", "one.etlab.in"),
            (other_college, "synthetic-alice", "two.etlab.in"),
        ):
            profile = await client.get("/v1/me/profile", headers=auth(token))
            assert profile.status_code == 200
            assert profile.json()["data"]["fields"] == {
                "Student": expected_student,
                "College": expected_college,
            }
        attendance = await client.get("/v1/me/attendance", headers=auth(alice))
        detail_id = attendance.json()["data"]["links"][0]["id"]
        materials = await client.get("/v1/me/materials", headers=auth(alice))
        file_id = materials.json()["data"]["links"][0]["id"]
        for unauthorized_token in (bob, other_college):
            detail = await client.get(
                f"/v1/me/attendance/{detail_id}", headers=auth(unauthorized_token)
            )
            file = await client.get(f"/v1/attachments/{file_id}", headers=auth(unauthorized_token))
            assert detail.status_code == file.status_code == 404
            assert detail.json()["error"]["code"] == file.json()["error"]["code"] == "not_found"
        assert (await client.get("/v1/me/profile", headers=auth())).status_code == 403
        assert portal.logins == 3


async def test_real_adapter_cache_retains_original_timestamp_and_refreshes_at_cutoff():
    clock = Clock()
    async with integration_client(clock=clock) as (client, portal, _):
        first = await client.get("/v1/me/attendance", headers=auth())
        assert first.status_code == 200 and not first.json()["meta"]["cache_hit"]
        original_time = first.json()["meta"]["fetched_at"]
        portal.attended = "21"
        clock.now += 59
        cached = await client.get("/v1/me/attendance", headers=auth())
        assert cached.json()["data"]["tables"][0]["rows"][0]["Attended"] == "20"
        assert cached.json()["meta"]["cache_hit"]
        assert cached.json()["meta"]["fetched_at"] == original_time
        clock.now += 1
        refreshed = await client.get("/v1/me/attendance", headers=auth())
        assert refreshed.json()["data"]["tables"][0]["rows"][0]["Attended"] == "21"
        assert not refreshed.json()["meta"]["cache_hit"]
        assert refreshed.json()["meta"]["fetched_at"] != original_time
        assert portal.counts[("one.etlab.in", "/student/attendance")] == 2


async def test_multiuser_upstream_expiry_revokes_token_and_does_not_reauthenticate():
    async with integration_client("multiuser") as (client, portal, _):
        token = await new_student_session(client, "synthetic-alice")
        portal.expire_all()
        expired = await client.get("/v1/me/profile", headers=auth(token))
        assert expired.status_code == 401 and expired.json()["error"]["code"] == "auth_expired"
        retry = await client.get("/v1/me/profile", headers=auth(token))
        assert retry.status_code == 401 and retry.json()["error"]["code"] == "unauthorized"
        assert portal.logins == 1
