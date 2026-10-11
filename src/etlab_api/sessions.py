"""Isolated in-memory upstream sessions, bounded caches, and login admission."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, TypeVar, cast

from etlab_api.errors import EtlabError
from etlab_api.models import AcademicPage, Capabilities, Download, SessionCreated, SessionInfo

if TYPE_CHECKING:
    from etlab_api.settings import Settings


class Adapter(Protocol):
    async def login(self, username: str, password: str) -> None: ...

    async def capabilities(self) -> Capabilities: ...

    async def read(
        self,
        resource: str,
        filters: dict[str, str] | None = None,
        detail_id: str | None = None,
    ) -> AcademicPage: ...

    async def download(self, attachment_id: str) -> Download: ...

    async def close(self) -> None: ...


AdapterFactory = Callable[[str, "Settings"], Adapter]
CacheKey = tuple[str, tuple[tuple[str, str], ...], str | None]
T = TypeVar("T")


@dataclass
class CacheEntry:
    value: AcademicPage | Capabilities
    fetched_at: float
    size_bytes: int


@dataclass
class ManagedSession:
    adapter: Adapter = field(repr=False)
    institution: str
    created_at: float
    last_used_at: float
    personal: bool = False
    ready: bool = False
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    cache: OrderedDict[CacheKey, CacheEntry] = field(default_factory=OrderedDict, repr=False)
    cache_bytes: int = 0


class SessionService:
    """One service per worker; credentials are retained only by personal Settings."""

    def __init__(
        self,
        settings: Settings,
        adapter_factory: AdapterFactory,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.adapter_factory = adapter_factory
        self.clock = clock
        self._sessions: dict[str, ManagedSession] = {}
        self._personal: ManagedSession | None = None
        self._registry_lock = asyncio.Lock()
        self._pending_logins = 0
        self._login_attempts: dict[str, deque[float]] = {}

    @staticmethod
    def _token_key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def is_deployment_token(self, token: str) -> bool:
        return secrets.compare_digest(
            token.encode(), self.settings.api_token.get_secret_value().encode()
        )

    @staticmethod
    def _clear_cache(session: ManagedSession) -> None:
        session.cache.clear()
        session.cache_bytes = 0

    def _expire_cache(self, session: ManagedSession) -> None:
        now = self.clock()
        expired = [
            key
            for key, entry in session.cache.items()
            if now - entry.fetched_at >= self.settings.cache_ttl_seconds
        ]
        for key in expired:
            session.cache_bytes -= session.cache.pop(key).size_bytes

    def _record_login_attempt(self, caller: str) -> None:
        """Caller holds the registry lock; do not trust forwarded IP headers."""
        now = self.clock()
        attempts = self._login_attempts.setdefault(caller, deque())
        while attempts and now - attempts[0] >= 60:
            attempts.popleft()
        if len(attempts) >= self.settings.login_attempts_per_minute:
            raise EtlabError("login_rate_limited", "Too many login attempts. Try later.", 429)
        attempts.append(now)

    def _expired(self, session: ManagedSession, now: float) -> bool:
        return not session.personal and (
            now - session.last_used_at >= self.settings.session_idle_seconds
            or now - session.created_at >= self.settings.session_max_seconds
        )

    async def _close(self, session: ManagedSession) -> None:
        async with session.lock:
            session.closed = True
            self._clear_cache(session)
            await session.adapter.close()

    async def cleanup(self) -> None:
        """Evict expired state even when no further requests use its token."""
        now = self.clock()
        async with self._registry_lock:
            expired = [
                key for key, session in self._sessions.items() if self._expired(session, now)
            ]
            sessions = [self._sessions.pop(key) for key in expired]
            for session in sessions:
                session.closed = True
            active = list(self._sessions.values())
            if self._personal is not None:
                active.append(self._personal)
            self._login_attempts = {
                key: attempts
                for key, attempts in self._login_attempts.items()
                if attempts and now - attempts[-1] < 60
            }
        for session in sessions:
            await self._close(session)
        for session in active:
            if session.lock.locked():
                continue
            async with session.lock:
                self._expire_cache(session)

    async def shutdown(self) -> None:
        async with self._registry_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            if self._personal is not None:
                sessions.append(self._personal)
                self._personal = None
            for session in sessions:
                session.closed = True
            self._login_attempts.clear()
        for session in sessions:
            await self._close(session)

    async def create(
        self, base_url: str, username: str, password: str, caller: str
    ) -> SessionCreated:
        if self.settings.app_mode != "multiuser":
            raise EtlabError("mode_mismatch", "Student sessions require multiuser mode.", 409)
        await self.cleanup()
        async with self._registry_lock:
            self._record_login_attempt(caller)
            if len(self._sessions) + self._pending_logins >= self.settings.max_sessions:
                raise EtlabError("session_capacity", "The session limit has been reached.", 503)
            self._pending_logins += 1
        adapter: Adapter | None = None
        try:
            adapter = self.adapter_factory(base_url, self.settings)
            await adapter.login(username, password)
            # Only the successful session keeps a cookie-bearing adapter. Passwords
            # are deliberately absent from ManagedSession and the cache.
            now = self.clock()
            institution = base_url.rstrip("/")
            session = ManagedSession(adapter, institution, now, now, ready=True)
            token = secrets.token_urlsafe(32)
            async with self._registry_lock:
                self._sessions[self._token_key(token)] = session
            return SessionCreated(
                session_token=token,
                expires_at=datetime.fromtimestamp(now + self.settings.session_max_seconds, UTC),
                idle_timeout_seconds=self.settings.session_idle_seconds,
                institution=institution,
            )
        except BaseException:
            if adapter is not None:
                await adapter.close()
            raise
        finally:
            async with self._registry_lock:
                self._pending_logins -= 1

    async def authenticate(self, token: str) -> ManagedSession:
        if self.settings.app_mode == "personal":
            if not self.is_deployment_token(token):
                raise EtlabError("unauthorized", "A valid bearer token is required.", 401)
            async with self._registry_lock:
                if self._personal is None:
                    now = self.clock()
                    self._personal = ManagedSession(
                        self.adapter_factory(self.settings.etlab_base_url, self.settings),
                        self.settings.etlab_base_url.rstrip("/"),
                        now,
                        now,
                        personal=True,
                    )
                return self._personal
        if self.is_deployment_token(token):
            raise EtlabError("session_token_required", "Use a student session bearer token.", 403)
        await self.cleanup()
        async with self._registry_lock:
            session = self._sessions.get(self._token_key(token))
            if session is None or session.closed:
                raise EtlabError("unauthorized", "The student session is invalid or expired.", 401)
            session.last_used_at = self.clock()
            return session

    def _require_active(self, session: ManagedSession) -> None:
        if session.closed or self._expired(session, self.clock()):
            raise EtlabError("auth_expired", "The student session has expired.", 401)

    async def _login_personal(self, session: ManagedSession) -> None:
        username = self.settings.etlab_username
        password = self.settings.etlab_password
        if username is None or password is None:
            raise EtlabError("configuration_error", "Personal ETLab credentials are missing.", 503)
        async with self._registry_lock:
            self._record_login_attempt("_personal")
        await session.adapter.login(username.get_secret_value(), password.get_secret_value())
        self._require_active(session)
        session.ready = True

    async def _perform(self, session: ManagedSession, operation: Callable[[], Awaitable[T]]) -> T:
        self._require_active(session)
        if session.personal and not session.ready:
            await self._login_personal(session)
        try:
            return await operation()
        except EtlabError as exc:
            if exc.code != "auth_expired":
                raise
            self._clear_cache(session)
            session.ready = False
            if session.personal:
                await self._login_personal(session)
                try:
                    return await operation()
                except EtlabError as retry_error:
                    if retry_error.code == "auth_expired":
                        session.ready = False
                    raise
            session.closed = True
            async with self._registry_lock:
                self._sessions = {
                    key: value for key, value in self._sessions.items() if value is not session
                }
            await session.adapter.close()
            raise

    async def _cached(
        self,
        session: ManagedSession,
        key: CacheKey,
        operation: Callable[[], Awaitable[AcademicPage | Capabilities]],
    ) -> tuple[AcademicPage | Capabilities, float, bool]:
        async with session.lock:
            self._require_active(session)
            self._expire_cache(session)
            now = self.clock()
            entry = session.cache.get(key)
            if entry is not None and now - entry.fetched_at < self.settings.cache_ttl_seconds:
                session.cache.move_to_end(key)
                return entry.value.model_copy(deep=True), entry.fetched_at, True
            if entry is not None:
                session.cache_bytes -= session.cache.pop(key).size_bytes
            result = await self._perform(session, operation)
            self._require_active(session)
            fetched_at = self.clock()
            # Bound response retention independently of the number of distinct
            # semester/filter combinations a caller can request.
            if self.settings.cache_ttl_seconds > 0:
                size_bytes = len(result.model_dump_json().encode())
                if size_bytes <= self.settings.cache_max_bytes:
                    session.cache[key] = CacheEntry(
                        result.model_copy(deep=True), fetched_at, size_bytes
                    )
                    session.cache_bytes += size_bytes
                    while (
                        len(session.cache) > 128
                        or session.cache_bytes > self.settings.cache_max_bytes
                    ):
                        _, removed = session.cache.popitem(last=False)
                        session.cache_bytes -= removed.size_bytes
            return result, fetched_at, False

    async def capabilities(self, session: ManagedSession) -> Capabilities:
        result, _, _ = await self._cached(
            session, ("_capabilities", (), None), session.adapter.capabilities
        )
        return cast(Capabilities, result)

    async def read(
        self,
        session: ManagedSession,
        resource: str,
        filters: dict[str, str] | None = None,
        detail_id: str | None = None,
    ) -> tuple[AcademicPage, float, bool]:
        normalized = tuple(sorted((filters or {}).items()))

        async def operation() -> AcademicPage:
            return await session.adapter.read(resource, dict(normalized), detail_id)

        result, fetched_at, hit = await self._cached(
            session, (resource, normalized, detail_id), operation
        )
        return cast(AcademicPage, result), fetched_at, hit

    async def download(self, session: ManagedSession, attachment_id: str) -> Download:
        async with session.lock:
            result = await self._perform(session, lambda: session.adapter.download(attachment_id))
            self._require_active(session)
            return result

    def info(self, session: ManagedSession) -> SessionInfo:
        self._require_active(session)
        expiry = None
        if not session.personal:
            expiry = datetime.fromtimestamp(
                min(
                    session.created_at + self.settings.session_max_seconds,
                    session.last_used_at + self.settings.session_idle_seconds,
                ),
                UTC,
            )
        return SessionInfo(
            mode="personal" if session.personal else "multiuser",
            institution=session.institution,
            expires_at=expiry,
        )

    async def delete(self, session: ManagedSession) -> None:
        async with self._registry_lock:
            session.closed = True
            if session.personal and self._personal is session:
                self._personal = None
            self._sessions = {
                key: value for key, value in self._sessions.items() if value is not session
            }
        await self._close(session)
