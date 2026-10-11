"""Bounded HTTPS requests with DNS validation bound to the connected socket.

The original hostname is retained by httpcore for TLS/SNI and certificate checking.
Only the TCP destination is replaced with an already validated public IP address.
"""

import asyncio
import ipaddress
import ssl
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend
from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream

from etlab_api.errors import EtlabError
from etlab_api.settings import Settings

Resolver = Callable[[str, int], Awaitable[list[str]]]
RedirectValidator = Callable[[str, str], None]


async def resolve_public(host: str, port: int) -> list[str]:
    # OS getaddrinfo runs in an executor thread that cannot be cancelled. Async
    # DNS keeps both resolution and application shutdown within the deadline.
    try:
        resolver = dns.asyncresolver.Resolver()

        async def lookup(kind: str) -> list[str]:
            try:
                answer = await resolver.resolve(host, kind, lifetime=5, search=False)
                return [record.to_text() for record in answer]
            except dns.resolver.NoAnswer:
                return []

        results = await asyncio.gather(lookup("A"), lookup("AAAA"), return_exceptions=True)
        addresses: list[str] = []
        for result in results:
            if isinstance(result, BaseException):
                raise EtlabError(
                    "upstream_unavailable", "ETLab hostname could not be resolved", 503
                )
            addresses.extend(result)
        return list(dict.fromkeys(addresses))
    except (dns.exception.DNSException, OSError) as exc:
        raise EtlabError(
            "upstream_unavailable", "ETLab hostname could not be resolved", 503
        ) from exc


def public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return ip.is_global and not ip.is_multicast and not ip.is_reserved
    except ValueError:
        return False


def canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        if parsed.port not in (None, 443) or parsed.fragment or "\\" in value:
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if host.endswith(".") or any(ch.isspace() for ch in value):
            raise ValueError
        # Hosts must be DNS names, not literal IPv4/IPv6 addresses.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        if ":" in host or not all(
            part and all(c.isalnum() or c == "-" for c in part) for part in host.split(".")
        ):
            raise ValueError
        return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError) as exc:
        raise EtlabError(
            "invalid_institution", "Use an allowed HTTPS ETLab hostname on port 443", 422
        ) from exc


def origin(value: str) -> str:
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}"


class HostPolicy:
    def __init__(self, allowed_hosts: Iterable[str], resolver: Resolver = resolve_public) -> None:
        self.allowed_hosts = tuple(pattern.strip().lower() for pattern in allowed_hosts)
        self.resolver = resolver

    def validate(self, url: str) -> str:
        clean = canonical_url(url)
        host = urlsplit(clean).hostname or ""
        allowed = any(
            host.endswith(pattern[1:]) and host != pattern[2:]
            if pattern.startswith("*.")
            else host == pattern
            for pattern in self.allowed_hosts
        )
        if not allowed:
            raise EtlabError("destination_denied", "Destination hostname is not allowed", 422)
        return clean

    async def addresses(self, host: str, port: int) -> list[str]:
        self.validate(f"https://{host}:{port}/")
        addresses = await self.resolver(host, port)
        # Reject mixed public/private DNS answers, rather than guessing which is intended.
        if not addresses or not all(public_address(address) for address in addresses):
            raise EtlabError(
                "destination_denied", "Destination must resolve only to public addresses", 422
            )
        return addresses


class PinnedBackend(AsyncNetworkBackend):
    def __init__(self, policy: HostPolicy, backend: AsyncNetworkBackend | None = None) -> None:
        self.policy = policy
        self.backend = backend or AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> AsyncNetworkStream:
        addresses = await self.policy.addresses(host, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self.backend.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore interface
        socket_options: Iterable[Any] | None = None,
    ) -> AsyncNetworkStream:
        raise EtlabError("destination_denied", "Unix socket destinations are not supported", 422)

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class CoreStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self.stream = stream

    async def __aiter__(self) -> Any:
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class SafeTransport(httpx.AsyncBaseTransport):
    def __init__(self, policy: HostPolicy) -> None:
        self.policy = policy
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=PinnedBackend(policy),
            max_connections=2,
            max_keepalive_connections=1,
            retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.policy.validate(str(request.url))
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = await self.pool.handle_async_request(core_request)
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=CoreStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.pool.aclose()


class UpstreamClient:
    """One cookie jar per actor; callers explicitly choose a read or login operation."""

    def __init__(
        self, base_url: str, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.policy = HostPolicy(settings.etlab_allowed_hosts)
        self.base_url = self.policy.validate(base_url).rstrip("/")
        if urlsplit(self.base_url).path or urlsplit(self.base_url).query:
            raise EtlabError(
                "invalid_institution", "Institution URL must contain only its HTTPS origin", 422
            )
        self.origin = origin(self.base_url)
        self.attachment_policy = HostPolicy(
            (urlsplit(self.base_url).hostname or "", *settings.etlab_attachment_hosts)
        )
        self.client = httpx.AsyncClient(
            transport=transport or SafeTransport(self.policy),
            timeout=settings.upstream_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": "ETLabAPI/0.1 (self-hosted student client)",
                "Accept-Encoding": "identity",
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.5",
            },
        )
        self.file_client = httpx.AsyncClient(
            transport=transport or SafeTransport(self.attachment_policy),
            timeout=settings.upstream_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept-Encoding": "identity"},
        )
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    def validate_attachment_url(self, url: str) -> str:
        """Validate an attachment origin without sending cookies or making a request."""
        return self.attachment_policy.validate(url)

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        data: Mapping[str, str] | None = None,
        attachment: bool = False,
        redirect_validator: RedirectValidator | None = None,
    ) -> httpx.Response:
        if method not in {"GET", "POST"}:
            raise EtlabError(
                "operation_denied", "Only declared read and login operations are supported", 405
            )
        if attachment and (method != "GET" or data):
            raise EtlabError("operation_denied", "Attachments are fetched with GET only", 405)
        policy = self.attachment_policy if attachment else self.policy
        target = policy.validate(urljoin(self.origin + "/", url))
        if not attachment and origin(target) != self.origin:
            raise EtlabError(
                "destination_denied", "Page requests must remain within this institution", 422
            )
        limit = self.settings.max_download_bytes if attachment else self.settings.max_html_bytes
        async with self._lock:
            delay = self.settings.upstream_interval_seconds - (
                time.monotonic() - self._last_request
            )
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                async with asyncio.timeout(self.settings.upstream_timeout_seconds):
                    for _ in range(6):
                        # An external file origin never receives institutional cookies or login data.
                        client = self.client if origin(target) == self.origin else self.file_client
                        async with client.stream(method, target, data=data) as response:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get("location")
                                if not location:
                                    raise EtlabError(
                                        "upstream_error", "ETLab returned an invalid redirect"
                                    )
                                next_url = policy.validate(urljoin(target, location))
                                if not attachment and origin(next_url) != self.origin:
                                    raise EtlabError(
                                        "destination_denied",
                                        "Cross-institution redirect denied",
                                        422,
                                    )
                                next_method, next_data = method, data
                                if method == "POST" and response.status_code in {301, 302, 303}:
                                    next_method, next_data = "GET", None
                                if next_data is not None and origin(next_url) != self.origin:
                                    raise EtlabError(
                                        "destination_denied", "Credential redirect denied", 422
                                    )
                                if redirect_validator is not None:
                                    redirect_validator(next_url, next_method)
                                target, method, data = next_url, next_method, next_data
                                continue
                            if response.status_code == 429:
                                raise EtlabError(
                                    "upstream_rate_limited",
                                    "ETLab rate limit reached; try later",
                                    503,
                                )
                            if response.status_code in {401, 403}:
                                raise EtlabError("upstream_denied", "ETLab denied access", 403)
                            if response.status_code >= 500:
                                raise EtlabError(
                                    "upstream_unavailable", "ETLab is temporarily unavailable", 503
                                )
                            if response.status_code >= 400:
                                raise EtlabError(
                                    "upstream_not_found", "ETLab resource is unavailable", 404
                                )
                            if (
                                response.headers.get("content-encoding", "identity").lower()
                                != "identity"
                            ):
                                raise EtlabError(
                                    "unsupported_encoding",
                                    "ETLab ignored the uncompressed response requirement",
                                )
                            declared = response.headers.get("content-length", "")
                            if declared.isdecimal() and int(declared) > limit:
                                raise EtlabError(
                                    "response_too_large",
                                    "ETLab response exceeds the configured size limit",
                                    413,
                                )
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(body) + len(chunk) > limit:
                                    raise EtlabError(
                                        "response_too_large",
                                        "ETLab response exceeds the configured size limit",
                                        413,
                                    )
                                body.extend(chunk)
                            return httpx.Response(
                                response.status_code,
                                headers=response.headers,
                                content=bytes(body),
                                request=response.request,
                            )
                    raise EtlabError("upstream_error", "ETLab exceeded the redirect limit")
            except (TimeoutError, httpx.TimeoutException, httpcore.TimeoutException) as exc:
                raise EtlabError("upstream_timeout", "ETLab did not respond in time", 504) from exc
            except (httpx.RequestError, httpcore.NetworkError, OSError) as exc:
                raise EtlabError("upstream_unavailable", "ETLab could not be reached", 503) from exc
            finally:
                self._last_request = time.monotonic()

    async def close(self) -> None:
        await self.client.aclose()
        await self.file_client.aclose()
