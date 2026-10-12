"""Small, interactive ETLab client. Run from the installed project's checkout."""

import argparse
import getpass
import ipaddress
import json
import os
import re
import sys
import warnings
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from etlab_api.models import RESOURCES, AcademicResponse, Capabilities, SessionCreated

# Only stable API codes may enter diagnostics; response messages can contain private data.
ERROR_CODES = frozenset(
    "auth_expired authentication_challenge configuration_error destination_denied http_error "
    "internal_error invalid_credentials invalid_filter invalid_filters invalid_institution "
    "invalid_request login_rate_limited mode_mismatch not_found operation_denied "
    "response_too_large session_capacity session_token_required unauthorized unsupported_encoding "
    "unsupported_feature unsupported_layout unsupported_resource upstream_denied upstream_error "
    "upstream_not_found upstream_rate_limited upstream_timeout upstream_unavailable".split()
)


class ClientFailure(Exception):
    """A deliberately safe diagnostic and process exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def validate_url(value: str, *, institution: bool = False) -> str:
    """Require a root origin; insecure HTTP is allowed only for a local API."""
    message = (
        "Use an HTTPS institution URL on port 443 with no credentials, query, fragment, or path."
        if institution
        else "Use an HTTPS API URL (HTTP is allowed for loopback only), with no credentials, "
        "query, fragment, or path."
    )
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if (
            not host
            or any(ord(char) <= 32 or char == "\\" for char in value)
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or "?" in value
            or "#" in value
            or parts.path not in {"", "/"}
            or port == 0
            or (institution and port not in {None, 443})
        ):
            raise ValueError
        loopback = host.lower() == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if parts.scheme != "https" and not (
            parts.scheme == "http" and loopback and not institution
        ):
            raise ValueError
        # Validate HTTPX's URL interpretation before any private input is requested.
        httpx.URL(value)
    except (ValueError, httpx.InvalidURL):
        raise ClientFailure(message, 2) from None
    return value.rstrip("/")


def hidden_input(label: str) -> str:
    # getpass normally falls back to echoed stdin. This example requires hidden input.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(label)
    except getpass.GetPassWarning:
        raise ClientFailure("Hidden input requires an interactive terminal.", 2) from None


def valid_token(value: str) -> bool:
    return bool(value) and len(value) <= 4096 and all(33 <= ord(char) <= 126 for char in value)


def error_diagnostic(response: httpx.Response) -> str:
    """Never include arbitrary bodies, messages, URLs, or header values in errors."""
    detail = f"HTTP {response.status_code}"
    try:
        body = response.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and code in ERROR_CODES:
            detail += f"; code={code}"
    except (ValueError, UnicodeError):
        pass
    request_id = response.headers.get("x-request-id", "")
    if re.fullmatch(r"[0-9a-f]{32}", request_id):
        detail += f"; request_id={request_id}"
    return detail


def request(
    client: httpx.Client,
    method: str,
    path: str,
    token: str,
    *,
    payload: dict[str, str] | None = None,
    cleanup: bool = False,
) -> httpx.Response:
    try:
        response = client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=5 if cleanup else 60,
        )
    except httpx.TimeoutException:
        raise ClientFailure("Request timed out; no retry was attempted.") from None
    except httpx.HTTPError:
        raise ClientFailure("Request failed; no retry was attempted.") from None
    expected = {204, 401} if cleanup else ({201} if method == "POST" else {200})
    if response.status_code not in expected:
        raise ClientFailure(error_diagnostic(response))
    return response


def decode[T: (Capabilities, AcademicResponse, SessionCreated)](
    response: httpx.Response, model: type[T]
) -> T:
    try:
        return model.model_validate(response.json())
    except (ValueError, UnicodeError, ValidationError):
        raise ClientFailure("Unexpected API response format.") from None


def read(
    client: httpx.Client,
    token: str,
    resource: str | None,
    show_data: bool,
) -> None:
    capabilities = decode(request(client, "GET", "/v1/capabilities", token), Capabilities)
    available = {
        item.resource
        for item in capabilities.features
        if item.available and item.resource in RESOURCES
    }
    if resource is None:
        print(
            json.dumps({"available_resources": [name for name in RESOURCES if name in available]})
        )
        return
    if resource not in available:
        raise ClientFailure(f"Resource '{resource}' is unavailable for this session.")
    page = decode(request(client, "GET", f"/v1/me/{resource}", token), AcademicResponse)
    if show_data:
        print(json.dumps(page.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return
    print(
        json.dumps(
            {
                "resource": resource,
                "available": True,
                "field_count": len(page.data.fields),
                "table_count": len(page.data.tables),
                "returned_rows": sum(len(table.rows) for table in page.data.tables),
                "total_rows": page.meta.total_rows,
                "link_count": len(page.data.links),
                "filter_count": len(page.data.filters),
                "empty": page.data.empty,
                "cache_hit": page.meta.cache_hit,
                "fetched_at": page.meta.fetched_at.isoformat(),
            }
        )
    )


def run(client: httpx.Client, args: argparse.Namespace, token: str) -> None:
    session_token = None
    primary_failed = False
    try:
        if args.mode == "multiuser":
            username = hidden_input("Student username: ")
            password = hidden_input("Student password: ")
            if not username.strip() or len(username) > 255 or not password or len(password) > 1024:
                raise ClientFailure("Enter a valid student username and password.", 2)
            login = request(
                client,
                "POST",
                "/v1/sessions",
                token,
                payload={"base_url": args.institution, "username": username, "password": password},
            )
            # Remember a valid token even if another login field is malformed, so cleanup runs.
            try:
                body = login.json()
                candidate = body.get("session_token") if isinstance(body, dict) else None
                if isinstance(candidate, str) and valid_token(candidate):
                    session_token = candidate
            except (ValueError, UnicodeError):
                pass
            decode(login, SessionCreated)
            if session_token is None:
                raise ClientFailure("Unexpected API session token format.")
        read(client, session_token or token, args.resource, args.show_data)
    except (ClientFailure, KeyboardInterrupt, EOFError):
        primary_failed = True
        raise
    finally:
        if session_token is not None:
            try:
                request(client, "DELETE", "/v1/session", session_token, cleanup=True)
            except ClientFailure as error:
                message = f"Session cleanup failed: {error}. The session will expire automatically."
                if primary_failed:
                    print(message, file=sys.stderr)
                else:
                    raise ClientFailure(message) from None


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("personal", "multiuser"), default="personal")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--institution", help="HTTPS institution URL; required in multiuser mode")
    parser.add_argument("--resource", choices=RESOURCES, help="Read one available resource")
    parser.add_argument(
        "--show-data", action="store_true", help="Print academic JSON; requires --resource"
    )
    args = parser.parse_args(argv)
    try:
        args.api_url = validate_url(args.api_url)
        if args.mode == "multiuser":
            if not args.institution:
                raise ClientFailure("Multiuser mode requires --institution.", 2)
            args.institution = validate_url(args.institution, institution=True)
        elif args.institution:
            raise ClientFailure("--institution applies only to multiuser mode.", 2)
        if args.show_data and not args.resource:
            raise ClientFailure("--show-data requires --resource.", 2)
        environment = os.environ if environ is None else environ
        token = environment.get("ETLAB_API_TOKEN")
        if token is None:
            token = hidden_input("Deployment API token: ")
        if not valid_token(token):
            raise ClientFailure("Enter a nonempty API token without whitespace.", 2)
        with httpx.Client(
            base_url=args.api_url,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=60,
        ) as client:
            run(client, args, token)
        return 0
    except ClientFailure as error:
        print(str(error), file=sys.stderr)
        return error.exit_code
    except EOFError:
        print("Input ended before all required secrets were entered.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
