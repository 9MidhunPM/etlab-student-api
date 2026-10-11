from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from etlab_api.adapters.student import MAX_DISCOVERED_LINKS, StudentAdapter
from etlab_api.errors import EtlabError
from etlab_api.models import RESOURCES

pytestmark = pytest.mark.anyio
FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://example.etlab.in"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FixtureClient:
    base_url = BASE

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], list[httpx.Response]] = defaultdict(list)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def page(
        self, path: str, fixture: str, *, method: str = "GET", final_path: str | None = None
    ) -> None:
        self.response(path, (FIXTURES / fixture).read_bytes(), method=method, final_path=final_path)

    def response(
        self,
        path: str,
        content: bytes,
        *,
        method: str = "GET",
        final_path: str | None = None,
        status: int = 200,
        content_type: str = "text/html",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.responses[(method, BASE + path)].append(
            httpx.Response(
                status,
                content=content,
                headers={"content-type": content_type, **(headers or {})},
                request=httpx.Request(method, BASE + (final_path or path)),
            )
        )

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        attachment: bool = False,
        redirect_validator: Callable[[str, str], None] | None = None,
    ) -> httpx.Response:
        self.calls.append({"url": url, "method": method, "data": data, "attachment": attachment})
        responses = self.responses[(method, url)]
        assert responses, f"Unexpected portal request: {method} {url}"
        return responses.pop(0)

    async def close(self) -> None:
        self.closed = True


async def logged_in() -> tuple[StudentAdapter, FixtureClient]:
    client = FixtureClient()
    client.page("/", "login.html")
    client.page("/user/login", "dashboard.html", method="POST", final_path="/student/index")
    adapter = StudentAdapter(client)
    await adapter.login("fixture-user", "fixture-password")
    return adapter, client


async def test_login_discovers_read_routes_and_preserves_csrf() -> None:
    adapter, client = await logged_in()
    assert client.calls[1]["data"] == {
        "YII_CSRF_TOKEN": "synthetic-csrf-value",
        "LoginForm[username]": "fixture-user",
        "LoginForm[password]": "fixture-password",
    }
    capabilities = await adapter.capabilities()
    assert {cap.resource for cap in capabilities.features if cap.available} == set(RESOURCES)
    assert capabilities.verification == "fixture_tested"
    assert all(feature.status != "live_verified" for feature in capabilities.features)
    assert all("logout" not in call["url"] for call in client.calls)


@pytest.mark.parametrize(
    ("controls", "expected"),
    [
        ("<button type='submit' name='yt0'>Login</button>", {"yt0": ""}),
        ("<input type='submit' name='login_button' value='Sign in'>", {"login_button": "Sign in"}),
        (
            "<button name='yt0'>Login</button><button type='reset' name='reset'>Reset</button><button type='button' name='cancel'>Cancel</button><button type='submit' name='disabled_submit' disabled>Login</button>",
            {"yt0": ""},
        ),
        (
            "<button type='submit' name='cancel' value='cancel'>Cancel</button><button type='submit' name='yt0'>Login</button>",
            {"yt0": ""},
        ),
    ],
)
async def test_login_preserves_only_the_selected_submit_controls(
    controls: str, expected: dict[str, str]
) -> None:
    client = FixtureClient()
    login_html = (
        "<form method='post' action='/user/login'><input name='LoginForm[username]'><input type='password' name='LoginForm[password]'>"
        + controls
        + "</form>"
    )
    client.response("/", login_html.encode())
    client.page("/user/login", "dashboard.html", method="POST", final_path="/student/index")
    await StudentAdapter(client).login("fixture-user", "fixture-password")
    assert client.calls[1]["data"] == {
        "LoginForm[username]": "fixture-user",
        "LoginForm[password]": "fixture-password",
        **expected,
    }


@pytest.mark.parametrize(
    "control",
    [
        "<button type='submit' name='username'>Login</button>",
        "<button type='submit' name='yt0' formaction='/student/delete'>Login</button>",
        "<button type='submit' name='yt0' formmethod='get'>Login</button>",
        "<button type='submit' name='one'>First</button><button type='submit' name='two'>Second</button>",
    ],
)
async def test_conflicting_or_unsafe_login_submit_controls_do_not_send_credentials(
    control: str,
) -> None:
    client = FixtureClient()
    client.response(
        "/",
        (
            "<form method='post' action='/user/login'><input name='username'><input name='password' type='password'>"
            + control
            + "</form>"
        ).encode(),
    )
    with pytest.raises(EtlabError) as raised:
        await StudentAdapter(client).login("fixture-user", "fixture-password")
    assert raised.value.code == "unsupported_layout"
    assert len(client.calls) == 1


async def test_detail_links_are_opaque_resource_and_account_bound() -> None:
    adapter, client = await logged_in()
    client.page("/student/attendance", "attendance.html")
    page = await adapter.read("attendance")
    assert len(page.links) == 2
    detail = page.links[0]
    assert detail.kind == "detail" and "482" not in detail.id
    client.response(
        "/ktuacademics/student/viewattendancesubject/482",
        b"<main><h1>Classes</h1><p>Lecture held: 2026-09-01</p></main>",
    )
    assert (await adapter.read("attendance", detail_id=detail.id)).text == [
        "Lecture held: 2026-09-01"
    ]
    with pytest.raises(EtlabError, match="does not belong") as invalid_resource:
        await adapter.read("results", detail_id=detail.id)
    assert invalid_resource.value.status == 404
    other_adapter, _ = await logged_in()
    with pytest.raises(EtlabError, match="does not belong"):
        await other_adapter.read("attendance", detail_id=detail.id)


async def test_discovered_get_filters_preserve_original_field_names_and_values() -> None:
    adapter, client = await logged_in()
    client.page("/student/attendance", "attendance.html")
    filtered_path = (
        "/student/attendance?YII_CSRF_TOKEN=synthetic-csrf-value&StudentAttendance%5Bsemester%5D=1"
    )
    client.page(filtered_path, "attendance.html")
    page = await adapter.read("attendance", {"StudentAttendance[semester]": "1"})
    assert page.filters[0].supported
    query = parse_qs(urlsplit(client.calls[-1]["url"]).query)
    assert query["StudentAttendance[semester]"] == ["1"]
    assert all(call["method"] == "GET" for call in client.calls[2:])


@pytest.mark.parametrize(
    "filters", [{"semester": "1"}, {"StudentAttendance[semester]": "999"}, {"action": "delete"}]
)
async def test_invalid_filters_never_reach_portal(filters: dict[str, str]) -> None:
    adapter, client = await logged_in()
    client.page("/student/attendance", "attendance.html")
    with pytest.raises(EtlabError) as raised:
        await adapter.read("attendance", filters)
    assert raised.value.code == "invalid_filter"
    assert len(client.calls) == 3


async def test_post_filter_not_replayed() -> None:
    adapter, client = await logged_in()
    client.response(
        "/student/surveys",
        b"<main><h1>Survey</h1><form method='post' action='/student/surveys'><select name='answer'><option value='1'>Yes</option></select></form></main>",
    )
    with pytest.raises(EtlabError) as raised:
        await adapter.read("surveys", {"answer": "1"})
    assert raised.value.code == "invalid_filter"
    assert len(client.calls) == 3


async def test_discovered_input_filters_validate_and_preserve_matched_form_defaults() -> None:
    adapter, client = await logged_in()
    client.page("/student/attendance", "date_filters.html")
    expected = {
        "YII_CSRF_TOKEN": "synthetic-csrf-value",
        "Attendance[from]": "2026-09-02",
        "Attendance[to]": "2026-09-30",
        "Attendance[search]": "Discrete mathematics",
        "Attendance[label]": "Term I",
        "Attendance[minimum_classes]": "4",
        "Attendance[starting_time]": "2026-09-02T10:30",
    }
    client.page("/student/attendance?" + urlencode(expected), "date_filters.html")
    page = await adapter.read(
        "attendance",
        {
            "Attendance[from]": "2026-09-02",
            "Attendance[search]": "Discrete mathematics",
            "Attendance[minimum_classes]": "4",
            "Attendance[starting_time]": "2026-09-02T10:30",
        },
    )
    assert all(item.supported for item in page.filters)
    assert parse_qs(urlsplit(client.calls[-1]["url"]).query) == {
        key: [value] for key, value in expected.items()
    }
    assert client.calls[-1]["method"] == "GET"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("Attendance[from]", "2026-02-30"),
        ("Attendance[from]", "20260201"),
        ("Attendance[from]", "2025-12-31"),
        ("Attendance[search]", "x" * 41),
        ("Attendance[label]", "x" * 513),
        ("Attendance[label]", "bad\nvalue"),
        ("Attendance[minimum_classes]", "NaN"),
        ("Attendance[minimum_classes]", "101"),
        ("Attendance[minimum_classes]", "-1"),
        ("Attendance[starting_time]", "2026-09-01T09:00Z"),
        ("Attendance[starting_time]", "2026-09-01T25:00"),
    ],
)
async def test_invalid_input_filter_values_are_not_sent(name: str, value: str) -> None:
    adapter, client = await logged_in()
    client.page("/student/attendance", "date_filters.html")
    with pytest.raises(EtlabError) as raised:
        await adapter.read("attendance", {name: value})
    assert raised.value.code == "invalid_filter"
    assert len(client.calls) == 3


@pytest.mark.parametrize(
    "unsafe_input",
    [
        "<input name='username' value='private'>",
        "<input name='action' value='delete'>",
        "<input name='csrf_token' type='text' value='private'>",
    ],
)
async def test_unsafe_filter_form_does_not_send_credentials_or_actions(unsafe_input: str) -> None:
    adapter, client = await logged_in()
    client.response(
        "/student/attendance",
        (
            "<main><h1>Attendance</h1><form method='get' action='/student/attendance'>"
            + unsafe_input
            + "<input name='search' value='Algorithms'></form></main>"
        ).encode(),
    )
    with pytest.raises(EtlabError) as raised:
        await adapter.read("attendance", {"search": "Mathematics"})
    assert raised.value.code == "invalid_filter"
    assert len(client.calls) == 3


async def test_http200_login_page_is_expired_session() -> None:
    adapter, client = await logged_in()
    client.page("/student/profile", "login.html")
    with pytest.raises(EtlabError) as raised:
        await adapter.read("profile")
    assert raised.value.code == "auth_expired"
    with pytest.raises(EtlabError) as expired:
        await adapter.capabilities()
    assert expired.value.status == 401


@pytest.mark.parametrize(
    ("status", "html", "code"),
    [
        (403, b"Forbidden", "upstream_denied"),
        (200, b"<main><h1>Forbidden</h1><p>You are not authorized</p></main>", "upstream_denied"),
        (200, b"<main><h1>Unexpected layout</h1></main>", "unsupported_layout"),
        (503, b"Unavailable", "upstream_unavailable"),
    ],
)
async def test_denied_missing_layout_and_upstream_failure_are_distinct(
    status: int, html: bytes, code: str
) -> None:
    adapter, client = await logged_in()
    client.response("/student/notices", html, status=status)
    with pytest.raises(EtlabError) as raised:
        await adapter.read("notices")
    assert raised.value.code == code


async def test_explicit_empty_is_a_successful_read() -> None:
    adapter, client = await logged_in()
    client.page("/student/notices", "empty.html")
    assert (await adapter.read("notices")).empty


async def test_attachment_downloads_are_opaque_and_reject_external_or_executable_links() -> None:
    adapter, client = await logged_in()
    client.page("/student/studymaterials", "materials.html")
    page = await adapter.read("materials")
    assert len(page.links) == 2
    assert all(link.kind == "attachment" for link in page.links)
    client.response(
        "/uploads/materials/example-notes.pdf",
        b"%PDF-1.4 synthetic",
        content_type="application/pdf",
        headers={"content-disposition": 'attachment; filename="../notes.pdf"'},
    )
    download = await adapter.download(page.links[0].id)
    assert download.content == b"%PDF-1.4 synthetic"
    assert download.filename == ".._notes.pdf"
    assert client.calls[-1]["attachment"]
    with pytest.raises(EtlabError) as unknown:
        await adapter.download("not-this-account")
    assert unknown.value.status == 404


async def test_download_detects_expiry_instead_of_returning_login_html() -> None:
    adapter, client = await logged_in()
    client.page("/student/studymaterials", "materials.html")
    page = await adapter.read("materials")
    client.page("/uploads/materials/example-notes.pdf", "login.html")
    with pytest.raises(EtlabError) as raised:
        await adapter.download(page.links[0].id)
    assert raised.value.code == "auth_expired"


async def test_external_attachments_require_an_explicit_client_policy() -> None:
    class ApprovedFileClient(FixtureClient):
        def validate_attachment_url(self, url: str) -> str:
            if urlsplit(url).netloc != "files.example.invalid":
                raise EtlabError("destination_denied", "Unapproved external file host", 422)
            return url

    client = ApprovedFileClient()
    client.page("/", "login.html")
    client.page("/user/login", "dashboard.html", method="POST", final_path="/student/index")
    adapter = StudentAdapter(client)
    await adapter.login("fixture-user", "fixture-password")
    client.response(
        "/student/studymaterials",
        b"<main><h1>Materials</h1><p>Lecture notes.</p><a href='https://files.example.invalid/notes.pdf'>Approved file</a><a href='https://attacker.invalid/notes.pdf'>Unapproved file</a><a href='https://files.example.invalid/delete'>Unreviewed action</a></main>",
    )
    page = await adapter.read("materials")
    assert len(page.links) == 1
    file_url = "https://files.example.invalid/notes.pdf"
    client.responses[("GET", file_url)].append(
        httpx.Response(
            200,
            content=b"%PDF synthetic",
            headers={"content-type": "application/pdf"},
            request=httpx.Request("GET", file_url),
        )
    )
    assert (await adapter.download(page.links[0].id)).content == b"%PDF synthetic"
    assert client.calls[-1]["attachment"]


@pytest.mark.parametrize(
    "html",
    [
        b"<form action='https://attacker.invalid/user/login' method='post'><input name='username'><input type='password' name='password'></form>",
        b"<form action='/student/submit' method='post'><input name='username'><input type='password' name='password'></form>",
    ],
)
async def test_unsafe_credential_destination_is_rejected_without_post(html: bytes) -> None:
    client = FixtureClient()
    client.response("/", html)
    with pytest.raises(EtlabError) as raised:
        await StudentAdapter(client).login("fixture-user", "fixture-password")
    assert raised.value.code == "unsupported_layout"
    assert len(client.calls) == 1


async def test_login_challenge_is_explicit_and_password_never_submitted() -> None:
    client = FixtureClient()
    client.response(
        "/",
        b"<form action='/user/login' method='post'><input name='username'><input type='password' name='password'><div class='g-recaptcha'></div></form>",
    )
    with pytest.raises(EtlabError) as raised:
        await StudentAdapter(client).login("fixture-user", "fixture-password")
    assert raised.value.code == "authentication_challenge"
    assert len(client.calls) == 1


async def test_close_invalidates_links_and_closes_client() -> None:
    adapter, client = await logged_in()
    await adapter.close()
    assert client.closed
    with pytest.raises(EtlabError):
        await adapter.read("profile")


def test_route_policy_rejects_mutations_encoded_routes_and_cross_origin() -> None:
    adapter = StudentAdapter(FixtureClient())
    for path in (
        "/student/delete/1",
        "/student/submitassignment/1",
        "/student/pay",
        "/user/logout",
        "/student/attendance/delete/1",
        "/student/%252e%252e/delete",
    ):
        assert adapter._resource(BASE + path) is None
    for path in (
        "/student/attendance?action=delete",
        "https://attacker.invalid/student/attendance",
        "//attacker.invalid/student/attendance",
        "#delete",
        "/student\\delete",
        "https://example.etlab.in:not-a-port/student/attendance",
        "https://[invalid/student/attendance",
    ):
        assert adapter._same_origin(path, BASE + "/student/index") is None


@pytest.mark.parametrize(
    "name",
    [
        "StudentAttendance[action]",
        "cmd[execute]",
        "student[redirect]",
        "deleteStudent",
        "StudentAttendance[%61ction]",
        "StudentAttendance[%2561ction]",
        "submitAttendance",
        "StudentAttendance[return_url]",
        "command",
        "_method",
    ],
)
def test_nested_encoded_or_camelcase_query_controls_are_rejected(name: str) -> None:
    adapter = StudentAdapter(FixtureClient())
    unsafe = "/student/attendance?" + urlencode({name: "1"})
    assert adapter._same_origin(unsafe, BASE + "/student/index") is None
    assert (
        adapter._same_origin(
            "/student/attendance?" + urlencode({"StudentAttendance[semester]": "2"}),
            BASE + "/student/index",
        )
        is not None
    )


async def test_link_registry_is_bounded_and_evicted_ids_are_not_found() -> None:
    adapter, _ = await logged_in()
    first = adapter._register_link(
        "attendance", BASE + "/student/attendance", "/student/viewattendance/0", "First"
    )
    assert first is not None
    for index in range(1, MAX_DISCOVERED_LINKS + 3):
        adapter._register_link(
            "attendance",
            BASE + "/student/attendance",
            f"/student/viewattendance/{index}",
            str(index),
        )
    assert len(adapter._links) == MAX_DISCOVERED_LINKS
    assert len(adapter._link_ids) == MAX_DISCOVERED_LINKS
    with pytest.raises(EtlabError) as raised:
        await adapter.read("attendance", detail_id=first.id)
    assert raised.value.code == "not_found"
    old_count = len(adapter._links)
    for index in range(3, MAX_DISCOVERED_LINKS + 3):
        adapter._register_link(
            "attendance",
            BASE + "/student/attendance",
            f"/student/viewattendance/{index}",
            str(index),
        )
    assert len(adapter._links) == old_count
    assert len(adapter._link_ids) == old_count


async def test_recently_used_link_survives_lru_eviction() -> None:
    adapter, client = await logged_in()
    first = adapter._register_link(
        "attendance", BASE + "/student/attendance", "/student/viewattendance/0", "First"
    )
    second = adapter._register_link(
        "attendance", BASE + "/student/attendance", "/student/viewattendance/1", "Second"
    )
    assert first is not None and second is not None
    for index in range(2, MAX_DISCOVERED_LINKS):
        adapter._register_link(
            "attendance",
            BASE + "/student/attendance",
            f"/student/viewattendance/{index}",
            str(index),
        )
    client.response(
        "/student/viewattendance/0", b"<main><h1>Attendance</h1><p>Lesson attended.</p></main>"
    )
    await adapter.read("attendance", detail_id=first.id)
    adapter._register_link(
        "attendance",
        BASE + "/student/attendance",
        f"/student/viewattendance/{MAX_DISCOVERED_LINKS}",
        "New",
    )
    assert first.id in adapter._links
    assert second.id not in adapter._links
    assert len(adapter._links) == len(adapter._link_ids) == MAX_DISCOVERED_LINKS


async def test_reauthentication_preserves_same_identity_ids_and_clears_new_identity() -> None:
    adapter, client = await logged_in()
    client.page("/student/attendance", "attendance.html")
    page = await adapter.read("attendance")
    detail = page.links[0]
    client.page("/", "login.html")
    client.page("/user/login", "dashboard.html", method="POST", final_path="/student/index")
    await adapter.login("fixture-user", "changed-fixture-password")
    assert detail.id in adapter._links
    client.response(
        "/ktuacademics/student/viewattendancesubject/482",
        b"<main><h1>Classes</h1><p>Attendance retained.</p></main>",
    )
    assert (await adapter.read("attendance", detail_id=detail.id)).text == ["Attendance retained."]
    assert adapter._identity != "fixture-user"
    client.page("/", "login.html")
    client.page("/user/login", "dashboard.html", method="POST", final_path="/student/index")
    await adapter.login("different-fixture-user", "fixture-password")
    with pytest.raises(EtlabError) as raised:
        await adapter.read("attendance", detail_id=detail.id)
    assert raised.value.code == "not_found"
    assert not adapter._links and not adapter._link_ids


def test_redirect_guards_accept_reviewed_reads_and_block_mutation_or_credential_routes() -> None:
    adapter = StudentAdapter(FixtureClient())
    adapter._login_redirect(BASE + "/student/index", "GET")
    adapter._login_redirect(BASE + "/user/login", "POST")
    adapter._read_redirect(BASE + "/user/login", "GET")
    adapter._read_redirect(BASE + "/student/attendance", "GET")
    adapter._attachment_redirect(BASE + "/uploads/notes.pdf", "GET")
    adapter._attachment_redirect(BASE + "/user/login", "GET")
    for guard, path, method in [
        (adapter._login_redirect, "/student/delete", "GET"),
        (adapter._login_redirect, "/student/index", "POST"),
        (adapter._read_redirect, "/student/delete", "GET"),
        (adapter._read_redirect, "/student/attendance?student[action]=delete", "GET"),
        (adapter._read_redirect, "/student/attendance", "POST"),
        (adapter._attachment_redirect, "/student/delete", "GET"),
        (adapter._attachment_redirect, "/uploads/notes.pdf", "POST"),
    ]:
        with pytest.raises(EtlabError) as raised:
            guard(BASE + path, method)
        assert raised.value.code == "operation_denied"


async def test_oversize_page_advertises_only_retained_ids() -> None:
    adapter, client = await logged_in()
    body = (
        "<main><h1>Attendance</h1>"
        + "".join(
            f"<a href='/student/viewattendance/{index}'>Lesson {index}</a>"
            for index in range(MAX_DISCOVERED_LINKS + 2)
        )
        + "</main>"
    )
    client.response("/student/attendance", body.encode())
    page = await adapter.read("attendance")
    assert len(page.links) == MAX_DISCOVERED_LINKS
    assert all(link.id in adapter._links for link in page.links)
