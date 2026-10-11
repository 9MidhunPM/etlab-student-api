"""Session lifecycle checks use isolated fake adapters, never real credentials."""

import asyncio
from collections.abc import Callable

import pytest

from etlab_api.errors import EtlabError
from etlab_api.models import AcademicPage, Capabilities, Download, Table
from etlab_api.sessions import SessionService
from etlab_api.settings import Settings

pytestmark = pytest.mark.anyio
TOKEN = "deployment-token-" + "x" * 32


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeAdapter:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.username = ""
        self.logins = 0
        self.reads = 0
        self.closed = False
        self.errors: list[str] = []
        self.login_error = False
        self.login_gate: asyncio.Event | None = None
        self.login_started = asyncio.Event()
        self.read_gate: asyncio.Event | None = None
        self.read_started = asyncio.Event()

    async def login(self, username: str, password: str) -> None:
        self.logins += 1
        self.login_started.set()
        if self.login_gate:
            await self.login_gate.wait()
        if self.login_error:
            raise EtlabError("login_failed", "ETLab credentials were rejected.", 401)
        self.username = username

    async def capabilities(self) -> Capabilities:
        return Capabilities(
            institution=self.base_url, adapter="fake", adapter_version="1", features=[]
        )

    async def read(self, resource: str, filters=None, detail_id=None) -> AcademicPage:
        self.reads += 1
        self.read_started.set()
        if self.read_gate:
            await self.read_gate.wait()
        await asyncio.sleep(0)
        if self.errors:
            raise EtlabError(self.errors.pop(0), "Session unavailable.", 401)
        return AcademicPage(
            title=resource,
            fields={"Student": self.username, "Institution": self.base_url},
            tables=[Table(columns=["Label"], rows=[{"Label": "first"}, {"Label": "second"}])],
        )

    async def download(self, attachment_id: str) -> Download:
        return Download(filename="test.txt", content_type="text/plain", content=b"file")

    async def close(self) -> None:
        self.closed = True


def setup_service(mode="multiuser", **overrides):
    settings = Settings(
        _env_file=None,
        api_token=TOKEN,
        app_mode=mode,
        etlab_username="personal-student",
        etlab_password="private-password",
        **overrides,
    )
    adapters: list[FakeAdapter] = []

    def factory(base_url: str, _) -> FakeAdapter:
        adapter = FakeAdapter(base_url)
        adapters.append(adapter)
        return adapter

    clock = Clock()
    return SessionService(settings, factory, clock), adapters, clock


async def assert_error(code: str, operation: Callable):
    with pytest.raises(EtlabError) as captured:
        await operation()
    assert captured.value.code == code


async def test_personal_lazy_login_concurrent_cache_and_independent_copies():
    service, adapters, _ = setup_service("personal")
    session = await service.authenticate(TOKEN)
    assert adapters[0].logins == 0
    results = await asyncio.gather(*(service.read(session, "attendance") for _ in range(8)))
    assert adapters[0].logins == adapters[0].reads == 1
    assert sum(hit for _, _, hit in results) == 7
    results[0][0].tables[0].rows.clear()
    page, _, cached = await service.read(session, "attendance")
    assert cached and len(page.tables[0].rows) == 2
    await service.shutdown()
    assert adapters[0].closed


async def test_cache_cutoff_and_normalized_filters():
    service, adapters, clock = setup_service("personal")
    session = await service.authenticate(TOKEN)
    _, fetched_at, hit = await service.read(
        session, "attendance", {"semester": "3", "subject": "2"}
    )
    assert not hit
    clock.now += 59.999
    _, original_time, hit = await service.read(
        session, "attendance", {"subject": "2", "semester": "3"}
    )
    assert hit and original_time == fetched_at
    clock.now = fetched_at + 60
    _, refreshed_time, hit = await service.read(
        session, "attendance", {"semester": "3", "subject": "2"}
    )
    assert not hit and refreshed_time == clock.now and adapters[0].reads == 2
    await service.read(session, "attendance", {"semester": "4"})
    assert adapters[0].reads == 3


async def test_cache_has_bounded_retention():
    service, _, _ = setup_service("personal")
    session = await service.authenticate(TOKEN)
    for value in range(130):
        await service.read(session, "attendance", {"semester": str(value)})
    assert len(session.cache) == 128


async def test_personal_reauth_once_and_clears_cached_data():
    service, adapters, _ = setup_service("personal")
    session = await service.authenticate(TOKEN)
    await service.read(session, "profile")
    adapters[0].errors = ["auth_expired"]
    await service.read(session, "attendance")
    assert adapters[0].logins == 2 and adapters[0].reads == 3
    _, _, hit = await service.read(session, "profile")
    assert not hit
    adapters[0].errors = ["auth_expired", "auth_expired"]
    await assert_error("auth_expired", lambda: service.read(session, "results"))
    assert adapters[0].logins == 3 and not session.ready


async def test_multiuser_data_tokens_isolate_users_and_institutions():
    service, adapters, _ = setup_service()
    first = await service.create("https://one.etlab.in", "student-one", "first-secret", "client")
    second = await service.create("https://one.etlab.in", "student-two", "second-secret", "client")
    third = await service.create("https://two.etlab.in", "student-one", "third-secret", "client")
    tokens = {first.session_token, second.session_token, third.session_token}
    assert len(tokens) == 3
    assert not any(token in service._sessions for token in tokens)
    await assert_error("session_token_required", lambda: service.authenticate(TOKEN))
    for created, expected_name, expected_host in (
        (first, "student-one", "https://one.etlab.in"),
        (second, "student-two", "https://one.etlab.in"),
        (third, "student-one", "https://two.etlab.in"),
    ):
        session = await service.authenticate(created.session_token)
        page, _, _ = await service.read(session, "profile")
        assert page.fields == {"Student": expected_name, "Institution": expected_host}
        assert not hasattr(session, "password")
    await service.shutdown()
    assert all(adapter.closed for adapter in adapters)


async def test_idle_boundary_cleanup_and_maximum_lifetime_with_activity():
    service, adapters, clock = setup_service(session_idle_seconds=10, session_max_seconds=30)
    created = await service.create("https://one.etlab.in", "student", "secret", "client")
    clock.now += 10
    await service.cleanup()
    assert adapters[0].closed and not service._sessions
    await assert_error("unauthorized", lambda: service.authenticate(created.session_token))
    second = await service.create("https://one.etlab.in", "student", "secret", "client")
    for _ in range(3):
        clock.now += 9
        await service.authenticate(second.session_token)
    clock.now += 3
    await assert_error("unauthorized", lambda: service.authenticate(second.session_token))
    assert adapters[1].closed


async def test_expired_upstream_multiuser_session_cannot_reauthenticate():
    service, adapters, _ = setup_service()
    created = await service.create("https://one.etlab.in", "student", "secret", "client")
    session = await service.authenticate(created.session_token)
    adapters[0].errors = ["auth_expired"]
    await assert_error("auth_expired", lambda: service.read(session, "attendance"))
    assert adapters[0].logins == 1 and adapters[0].closed
    await assert_error("unauthorized", lambda: service.authenticate(created.session_token))


async def test_login_failure_discards_adapter_and_capacity_reservation():
    service, adapters, _ = setup_service(max_sessions=1)

    def failing_factory(base_url, _):
        adapter = FakeAdapter(base_url)
        adapter.login_error = True
        adapters.append(adapter)
        return adapter

    service.adapter_factory = failing_factory
    await assert_error(
        "login_failed",
        lambda: service.create("https://one.etlab.in", "student", "secret", "client"),
    )
    assert adapters[0].closed and service._pending_logins == 0 and not service._sessions


async def test_concurrent_login_admission_reserves_capacity():
    service, adapters, _ = setup_service(max_sessions=1)
    gate = asyncio.Event()

    def delayed_factory(base_url, _):
        adapter = FakeAdapter(base_url)
        adapter.login_gate = gate
        adapters.append(adapter)
        return adapter

    service.adapter_factory = delayed_factory
    task = asyncio.create_task(
        service.create("https://one.etlab.in", "student", "secret", "first-client")
    )
    await asyncio.sleep(0)
    await adapters[0].login_started.wait()
    await assert_error(
        "session_capacity",
        lambda: service.create("https://one.etlab.in", "student", "secret", "second-client"),
    )
    gate.set()
    await task
    assert len(service._sessions) == 1 and service._pending_logins == 0


async def test_login_throttle_and_expired_buckets():
    service, _, clock = setup_service(login_attempts_per_minute=1)
    created = await service.create("https://one.etlab.in", "student", "secret", "client")
    await assert_error(
        "login_rate_limited",
        lambda: service.create("https://one.etlab.in", "student", "secret", "client"),
    )
    clock.now += 60
    await service.create("https://one.etlab.in", "student", "secret", "client")
    await service.delete(await service.authenticate(created.session_token))
    await assert_error("unauthorized", lambda: service.authenticate(created.session_token))


async def test_delete_personal_discards_cookies_and_restarts_lazy_session():
    service, adapters, _ = setup_service("personal")
    session = await service.authenticate(TOKEN)
    await service.read(session, "profile")
    await service.delete(session)
    assert session.closed and not session.cache and adapters[0].closed
    replacement = await service.authenticate(TOKEN)
    assert replacement is not session and adapters[1].logins == 0


async def test_cleanup_evicts_expired_cache_for_active_and_personal_sessions():
    for mode in ("personal", "multiuser"):
        service, _, clock = setup_service(mode)
        token = TOKEN
        if mode == "multiuser":
            token = (
                await service.create("https://one.etlab.in", "student", "secret", "client")
            ).session_token
        session = await service.authenticate(token)
        await service.read(session, "profile")
        assert session.cache and session.cache_bytes > 0
        clock.now += 60
        await service.cleanup()
        assert not session.cache and session.cache_bytes == 0 and not session.closed


async def test_byte_budget_bypasses_large_entries_and_evicts_oldest():
    service, adapters, _ = setup_service("personal", cache_max_bytes=1)
    session = await service.authenticate(TOKEN)
    await service.read(session, "profile")
    _, _, hit = await service.read(session, "profile")
    assert not hit and adapters[0].reads == 2
    assert not session.cache and session.cache_bytes == 0
    other, _, _ = setup_service("personal", cache_max_bytes=300)
    other_session = await other.authenticate(TOKEN)
    await other.read(other_session, "profile")
    await other.read(other_session, "attendance")
    assert 0 < other_session.cache_bytes <= 300
    assert len(other_session.cache) < 2
    assert next(iter(other_session.cache))[0] == "attendance"


async def test_non_ascii_token_is_rejected_without_comparison_error():
    service, _, _ = setup_service("personal")
    assert not service.is_deployment_token("wrong-é-token")
    await assert_error("unauthorized", lambda: service.authenticate("wrong-é-token"))


async def test_failed_personal_login_is_throttled_without_reauthentication_loop():
    service, adapters, clock = setup_service("personal", login_attempts_per_minute=1)
    session = await service.authenticate(TOKEN)
    adapters[0].login_error = True
    await assert_error("login_failed", lambda: service.read(session, "profile"))
    await assert_error("login_rate_limited", lambda: service.read(session, "profile"))
    assert adapters[0].logins == 1 and not session.ready
    clock.now += 60
    adapters[0].login_error = False
    await service.read(session, "profile")
    assert adapters[0].logins == 2


async def test_expired_during_fetch_never_caches_or_returns_result():
    service, adapters, clock = setup_service(session_idle_seconds=10, session_max_seconds=20)
    created = await service.create("https://one.etlab.in", "student", "secret", "client")
    session = await service.authenticate(created.session_token)

    async def read_then_expire(resource, filters=None, detail_id=None):
        clock.now += 10
        return AcademicPage(title="expired-result")

    adapters[0].read = read_then_expire
    await assert_error("auth_expired", lambda: service.read(session, "profile"))
    assert not session.cache


async def test_delete_revokes_an_in_flight_read_before_return():
    service, adapters, _ = setup_service()
    created = await service.create("https://one.etlab.in", "student", "secret", "client")
    session = await service.authenticate(created.session_token)
    gate = asyncio.Event()
    adapters[0].read_gate = gate
    task = asyncio.create_task(service.read(session, "profile"))
    await adapters[0].read_started.wait()
    deleting = asyncio.create_task(service.delete(session))
    await asyncio.sleep(0)
    assert session.closed
    gate.set()
    with pytest.raises(EtlabError, match="expired"):
        await task
    await deleting
    assert adapters[0].closed and not session.cache


async def test_expired_during_download_never_returns_bytes():
    service, adapters, clock = setup_service(session_idle_seconds=10, session_max_seconds=20)
    created = await service.create("https://one.etlab.in", "student", "secret", "client")
    session = await service.authenticate(created.session_token)

    async def download_then_expire(attachment_id):
        clock.now += 10
        return Download(filename="private.txt", content_type="text/plain", content=b"private")

    adapters[0].download = download_then_expire
    await assert_error("auth_expired", lambda: service.download(session, "attachment"))
