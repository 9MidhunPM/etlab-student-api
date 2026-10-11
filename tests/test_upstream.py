import asyncio
import gzip
from typing import Any

import dns.exception
import dns.resolver
import httpcore
import httpx
import pytest

from etlab_api.errors import EtlabError
from etlab_api.settings import Settings
from etlab_api.upstream import (
    HostPolicy,
    PinnedBackend,
    UpstreamClient,
    public_address,
    resolve_public,
)

pytestmark = pytest.mark.anyio


def settings(**kwargs: Any) -> Settings:
    return Settings(
        _env_file=None,
        app_mode="multiuser",
        api_token="x" * 43,
        upstream_interval_seconds=0,
        **kwargs,
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "169.254.169.254",
        "::ffff:127.0.0.1",
        "224.0.0.1",
        "192.0.2.1",
        "fc00::1",
    ],
)
def test_nonpublic_addresses_denied(address: str) -> None:
    assert not public_address(address)


@pytest.mark.parametrize(
    "url",
    [
        "http://sahrdaya.etlab.in",
        "https://127.0.0.1/",
        "https://sahrdaya.etlab.in.evil.example/",
        "https://sahrdaya.etlab.in:444/",
        "https://u:p@sahrdaya.etlab.in/",
        "https://sahrdaya.etlab.in./",
        "https://etlab.in/",
        "https://sahrdaya.etlab.in/#fragment",
        "https://sahrdaya.etlab.in\\@evil.example/",
    ],
)
def test_url_policy(url: str) -> None:
    with pytest.raises(EtlabError):
        HostPolicy(("*.etlab.in",)).validate(url)


async def test_dns_pinning_and_mixed_answers() -> None:
    calls = []

    async def resolver(host: str, port: int) -> list[str]:
        return ["1.1.1.1"]

    class Backend:
        async def connect_tcp(self, *args: Any) -> str:
            calls.append(args)
            return "stream"

    backend = PinnedBackend(HostPolicy(("*.etlab.in",), resolver), Backend())
    assert await backend.connect_tcp("sahrdaya.etlab.in", 443) == "stream"
    assert calls[0][0] == "1.1.1.1"

    async def mixed(host: str, port: int) -> list[str]:
        return ["1.1.1.1", "10.0.0.1"]

    with pytest.raises(EtlabError) as error:
        await HostPolicy(("*.etlab.in",), mixed).addresses("sahrdaya.etlab.in", 443)
    assert error.value.code == "destination_denied"


async def test_cookies_never_cross_attachment_origin() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/login":
            return httpx.Response(
                200,
                headers={"Set-Cookie": "session=private; Domain=.etlab.in; Secure; Path=/"},
                content=b"ok",
            )
        return httpx.Response(200, content=b"file")

    client = UpstreamClient(
        "https://sahrdaya.etlab.in",
        settings(etlab_attachment_hosts="files.etlab.in"),
        httpx.MockTransport(handler),
    )
    try:
        await client.fetch("/login")
        await client.fetch("/document", attachment=True)
        await client.fetch("https://files.etlab.in/document", attachment=True)
        assert "session=private" in seen[1].headers.get("cookie", "")
        assert "cookie" not in seen[2].headers
    finally:
        await client.close()


async def test_redirect_policy_before_next_request() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(307, headers={"location": "https://mace.etlab.in/user/login"})

    client = UpstreamClient("https://sahrdaya.etlab.in", settings(), httpx.MockTransport(handler))
    try:
        with pytest.raises(EtlabError) as error:
            await client.fetch("/user/login", method="POST", data={"password": "private"})
        assert error.value.code == "destination_denied"
        assert len(seen) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("status,method", [(302, "GET"), (307, "POST")])
async def test_operation_redirect_guard_before_next_request(status: int, method: str) -> None:
    seen = []
    checked = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, headers={"location": "/student/delete?id=123"})

    def guard(url: str, next_method: str) -> None:
        checked.append((url, next_method))
        raise EtlabError("operation_denied", "Unreviewed redirect", 405)

    client = UpstreamClient("https://sahrdaya.etlab.in", settings(), httpx.MockTransport(handler))
    try:
        with pytest.raises(EtlabError) as error:
            await client.fetch(
                "/user/login", method="POST", data={"password": "private"}, redirect_validator=guard
            )
        assert error.value.code == "operation_denied"
        assert checked == [("https://sahrdaya.etlab.in/student/delete?id=123", method)]
        assert len(seen) == 1
    finally:
        await client.close()


async def test_async_dns_answers_and_no_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    class Record:
        def to_text(self) -> str:
            return "1.1.1.1"

    class Resolver:
        async def resolve(self, host: str, kind: str, **kwargs: Any) -> list[Record]:
            seen.append((host, kind, kwargs))
            if kind == "AAAA":
                raise dns.resolver.NoAnswer
            return [Record(), Record()]

    monkeypatch.setattr("etlab_api.upstream.dns.asyncresolver.Resolver", Resolver)
    assert await resolve_public("sahrdaya.etlab.in", 443) == ["1.1.1.1"]
    assert {entry[1] for entry in seen} == {"A", "AAAA"}
    assert all(entry[2] == {"lifetime": 5, "search": False} for entry in seen)


async def test_async_dns_error_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    class Resolver:
        async def resolve(self, *args: Any, **kwargs: Any) -> None:
            raise dns.exception.Timeout("private detail")

    monkeypatch.setattr("etlab_api.upstream.dns.asyncresolver.Resolver", Resolver)
    with pytest.raises(EtlabError) as error:
        await resolve_public("sahrdaya.etlab.in", 443)
    assert error.value.code == "upstream_unavailable"
    assert "private detail" not in error.value.message


async def test_async_dns_cancelled_with_request_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped = []

    class Resolver:
        async def resolve(self, host: str, kind: str, **kwargs: Any) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(kind)

    monkeypatch.setattr("etlab_api.upstream.dns.asyncresolver.Resolver", Resolver)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await resolve_public("sahrdaya.etlab.in", 443)
    assert set(stopped) == {"A", "AAAA"}


@pytest.mark.parametrize("declared", [True, False])
async def test_download_limit(declared: bool) -> None:
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            yield b"1234"
            yield b"5678"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Length": "8"} if declared else {}, stream=Stream()
        )

    client = UpstreamClient(
        "https://sahrdaya.etlab.in", settings(max_download_bytes=5), httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(EtlabError) as error:
            await client.fetch("/file", attachment=True)
        assert error.value.code == "response_too_large"
    finally:
        await client.close()


async def test_compressed_response_rejected_without_decompression() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, content=gzip.compress(b"x" * 1000)
        )

    client = UpstreamClient("https://sahrdaya.etlab.in", settings(), httpx.MockTransport(handler))
    try:
        with pytest.raises(EtlabError) as error:
            await client.fetch("/page")
        assert error.value.code == "unsupported_encoding"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "exception,code",
    [
        (httpcore.ConnectError("private detail"), "upstream_unavailable"),
        (httpcore.ReadTimeout("private detail"), "upstream_timeout"),
    ],
)
async def test_network_errors_safe(exception: Exception, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    client = UpstreamClient("https://sahrdaya.etlab.in", settings(), httpx.MockTransport(handler))
    try:
        with pytest.raises(EtlabError) as error:
            await client.fetch("/page")
        assert error.value.code == code
        assert "private detail" not in error.value.message
    finally:
        await client.close()
