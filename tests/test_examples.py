"""Verify the runnable client's workflow using synthetic responses only."""

import importlib.util
import json
import warnings
from pathlib import Path

import httpx
import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "read_student.py"
spec = importlib.util.spec_from_file_location("read_student", EXAMPLE)
assert spec is not None and spec.loader is not None
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)

OPERATOR_TOKEN = "synthetic-operator-token-" + "x" * 32
STUDENT_TOKEN = "synthetic-student-token-" + "y" * 32
USERNAME = "private-synthetic-student"
PASSWORD = "private-synthetic-password"
INSTITUTION = "https://synthetic.etlab.in"
MULTIUSER = ["--mode", "multiuser", "--institution", INSTITUTION]
REQUEST_ID = "a" * 32


def capabilities(*, available=True):
    return {
        "institution": INSTITUTION,
        "adapter": "synthetic",
        "adapter_version": "1",
        "features": [
            {"resource": "attendance", "available": available, "status": "discovered"},
            {"resource": "fees", "available": False, "status": "unavailable"},
            {"resource": USERNAME, "available": True, "status": "discovered"},
        ],
    }


def academic(*, empty=False):
    return {
        "data": {
            "title": USERNAME,
            "fields": {} if empty else {"Student": USERNAME},
            "tables": [] if empty else [{"columns": ["Student"], "rows": [{"Student": USERNAME}]}],
            "text": [],
            "links": [],
            "filters": [],
            "empty": empty,
        },
        "meta": {
            "institution": INSTITUTION,
            "fetched_at": "2026-10-12T00:00:00Z",
            "cache_hit": True,
            "total_rows": 0 if empty else 1,
        },
    }


def login():
    return {
        "session_token": STUDENT_TOKEN,
        "expires_at": "2026-10-12T08:00:00Z",
        "idle_timeout_seconds": 1800,
        "institution": INSTITUTION,
    }


def api_error(status, code="unauthorized"):
    return httpx.Response(
        status,
        json={"error": {"code": code, "message": PASSWORD, "request_id": REQUEST_ID}},
        headers={"x-request-id": REQUEST_ID},
    )


def prompts(monkeypatch, values=(USERNAME, PASSWORD)):
    answers = iter(values)
    monkeypatch.setattr(example, "hidden_input", lambda _: next(answers))


def execute(handler, args=(), environ=None):
    return example.main(
        list(args),
        transport=httpx.MockTransport(handler),
        environ={"ETLAB_API_TOKEN": OPERATOR_TOKEN} if environ is None else environ,
    )


def assert_private(captured):
    output = captured.out + captured.err
    for secret in (OPERATOR_TOKEN, STUDENT_TOKEN, USERNAME, PASSWORD):
        assert secret not in output


def test_personal_lists_only_known_available_resources_and_preserves_session(capsys):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=capabilities())

    assert execute(handler) == 0
    assert [(request.method, request.url.path) for request in calls] == [
        ("GET", "/v1/capabilities")
    ]
    assert calls[0].headers["Authorization"] == f"Bearer {OPERATOR_TOKEN}"
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"available_resources": ["attendance"]}
    assert captured.err == ""
    assert_private(captured)


@pytest.mark.parametrize("empty", [False, True])
def test_resource_is_capability_first_and_prints_private_safe_summary(empty, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        body = capabilities() if request.url.path == "/v1/capabilities" else academic(empty=empty)
        return httpx.Response(200, json=body)

    assert execute(handler, ["--resource", "attendance"]) == 0
    assert [request.url.path for request in calls] == ["/v1/capabilities", "/v1/me/attendance"]
    assert all(request.method == "GET" for request in calls)
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["resource"] == "attendance" and summary["available"]
    assert summary["empty"] is empty and summary["cache_hit"]
    assert summary["returned_rows"] == summary["total_rows"] == (0 if empty else 1)
    assert summary["field_count"] == summary["table_count"] == (0 if empty else 1)
    assert summary["fetched_at"] == "2026-10-12T00:00:00+00:00"
    assert_private(captured)


def test_unavailable_resource_is_never_requested(capsys):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=capabilities(available=False))

    assert execute(handler, ["--resource", "attendance"]) == 1
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert "unavailable" in captured.err and captured.out == ""
    assert_private(captured)


def test_show_data_requires_explicit_opt_in(capsys):
    def handler(request):
        body = capabilities() if request.url.path == "/v1/capabilities" else academic()
        return httpx.Response(200, json=body)

    assert execute(handler, ["--resource", "attendance", "--show-data"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["fields"]["Student"] == USERNAME
    assert "session_token" not in output


def test_multiuser_uses_separate_token_roles_and_bounded_cleanup(monkeypatch, capsys):
    prompts(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert request.headers["Authorization"] == f"Bearer {OPERATOR_TOKEN}"
            assert json.loads(request.content) == {
                "base_url": INSTITUTION,
                "username": USERNAME,
                "password": PASSWORD,
            }
            return httpx.Response(201, json=login())
        assert request.headers["Authorization"] == f"Bearer {STUDENT_TOKEN}"
        if request.method == "DELETE":
            assert all(value == 5 for value in request.extensions["timeout"].values())
            return httpx.Response(204)
        assert all(value == 60 for value in request.extensions["timeout"].values())
        body = capabilities() if request.url.path == "/v1/capabilities" else academic()
        return httpx.Response(200, json=body)

    assert execute(handler, [*MULTIUSER, "--resource", "attendance"]) == 0
    assert [(request.method, request.url.path) for request in calls] == [
        ("POST", "/v1/sessions"),
        ("GET", "/v1/capabilities"),
        ("GET", "/v1/me/attendance"),
        ("DELETE", "/v1/session"),
    ]
    assert_private(capsys.readouterr())


def test_failed_login_never_reads_or_deletes_and_never_retries(monkeypatch, capsys):
    prompts(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        return api_error(429, "login_rate_limited")

    assert execute(handler, MULTIUSER) == 1
    assert len(calls) == 1 and calls[0].method == "POST"
    captured = capsys.readouterr()
    assert f"HTTP 429; code=login_rate_limited; request_id={REQUEST_ID}" in captured.err
    assert_private(captured)


@pytest.mark.parametrize("cleanup_status", [204, 401, 503])
def test_cleanup_after_primary_failure_does_not_hide_primary_error(
    cleanup_status, monkeypatch, capsys
):
    prompts(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(201, json=login())
        if request.method == "DELETE":
            return api_error(cleanup_status)
        return api_error(502, "unsupported_layout")

    assert execute(handler, MULTIUSER) == 1
    assert [request.method for request in calls] == ["POST", "GET", "DELETE"]
    captured = capsys.readouterr()
    assert "HTTP 502; code=unsupported_layout" in captured.err
    assert ("Session cleanup failed" in captured.err) == (cleanup_status == 503)
    assert_private(captured)


@pytest.mark.parametrize("cleanup_status", [204, 401, 503])
def test_cleanup_status_changes_success_exit_code(cleanup_status, monkeypatch, capsys):
    prompts(monkeypatch)

    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json=login())
        if request.method == "DELETE":
            return api_error(cleanup_status)
        return httpx.Response(200, json=capabilities())

    assert execute(handler, MULTIUSER) == (1 if cleanup_status == 503 else 0)
    captured = capsys.readouterr()
    assert ("Session cleanup failed" in captured.err) == (cleanup_status == 503)
    assert_private(captured)


@pytest.mark.parametrize("primary", [False, True])
def test_cleanup_transport_failure_is_safe_and_bounded(primary, monkeypatch, capsys):
    prompts(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(201, json=login())
        if request.method == "DELETE":
            assert request.extensions["timeout"]["read"] == 5
            raise httpx.ReadTimeout(PASSWORD, request=request)
        return api_error(401) if primary else httpx.Response(200, json=capabilities())

    assert execute(handler, MULTIUSER) == 1
    assert [request.method for request in calls] == ["POST", "GET", "DELETE"]
    captured = capsys.readouterr()
    assert "Session cleanup failed: Request timed out" in captured.err
    if primary:
        assert "HTTP 401" in captured.err
    assert_private(captured)


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError]
)
def test_transport_errors_are_safe_and_not_retried(failure, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        raise failure(PASSWORD, request=request)

    assert execute(handler) == 1
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert "no retry was attempted" in captured.err
    assert_private(captured)


@pytest.mark.parametrize("body", [None, [], {"features": "private-value"}, {"features": []}])
def test_malformed_capabilities_are_protocol_failures(body, capsys):
    def handler(_):
        return (
            httpx.Response(200, text=PASSWORD) if body is None else httpx.Response(200, json=body)
        )

    assert execute(handler) == 1
    captured = capsys.readouterr()
    assert "Unexpected API response format" in captured.err
    assert_private(captured)


def test_malformed_academic_response_does_not_print_private_contents(capsys):
    def handler(request):
        body = capabilities() if request.url.path == "/v1/capabilities" else {"data": PASSWORD}
        return httpx.Response(200, json=body)

    assert execute(handler, ["--resource", "attendance"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "Unexpected API response format" in captured.err
    assert_private(captured)


def test_malformed_login_with_valid_token_still_cleans_up(monkeypatch, capsys):
    prompts(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"session_token": STUDENT_TOKEN})
        assert request.headers["Authorization"] == f"Bearer {STUDENT_TOKEN}"
        return httpx.Response(204)

    assert execute(handler, MULTIUSER) == 1
    assert [request.method for request in calls] == ["POST", "DELETE"]
    assert_private(capsys.readouterr())


@pytest.mark.parametrize("token", ["", "contains whitespace", "private\nvalue", None])
def test_malformed_login_token_never_enters_request_headers(token, monkeypatch, capsys):
    prompts(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        body = login()
        body["session_token"] = token
        return httpx.Response(201, json=body)

    assert execute(handler, MULTIUSER) == 1
    assert len(calls) == 1
    assert_private(capsys.readouterr())


def test_arbitrary_error_codes_and_request_ids_are_never_printed(capsys):
    def handler(_):
        return httpx.Response(
            500,
            json={"error": {"code": USERNAME, "message": PASSWORD, "request_id": STUDENT_TOKEN}},
            headers={"x-request-id": STUDENT_TOKEN},
        )

    assert execute(handler) == 1
    captured = capsys.readouterr()
    assert captured.err == "HTTP 500\n"
    assert_private(captured)


def test_non_json_http_error_never_prints_response_body(capsys):
    assert execute(lambda _: httpx.Response(502, text=PASSWORD)) == 1
    captured = capsys.readouterr()
    assert captured.err == "HTTP 502\n"
    assert_private(captured)


def test_redirect_is_not_followed_and_location_is_not_printed(capsys):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": f"https://other.example/{PASSWORD}"})

    assert execute(handler) == 1
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert captured.err == "HTTP 307\n"
    assert_private(captured)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example",
        "http://127.0.0.1.example",
        "https://private-user:private-pass@api.example",
        "https://api.example?private-query",
        "https://api.example#private-fragment",
        "https://api.example/child",
        "https://api.example?",
        "https://api.example#",
        "ftp://127.0.0.1",
        "https://api.example:invalid",
        "https://api.example:99999",
        "https://api.example:0",
        " https://api.example",
        "https://api.example\n",
        "https://api.example\\child",
        "https:///",
    ],
)
def test_invalid_api_urls_fail_before_prompts_or_network(url, monkeypatch, capsys):
    def forbidden(_):
        pytest.fail("Invalid configuration must not prompt or request")

    monkeypatch.setattr(example, "hidden_input", forbidden)
    assert execute(forbidden, ["--api-url", url], environ={}) == 2
    captured = capsys.readouterr()
    assert "Use an HTTPS API URL" in captured.err and url not in captured.err


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://127.0.0.2:8000/",
        "http://[::1]:8000",
        "https://api.example",
        "https://api.example:8443",
    ],
)
def test_valid_urls_reach_only_the_given_origin(url, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=capabilities())

    assert execute(handler, ["--api-url", url]) == 0
    assert len(calls) == 1 and calls[0].url.path == "/v1/capabilities"
    assert calls[0].url.port == httpx.URL(url).port
    assert_private(capsys.readouterr())


@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "multiuser"],
        ["--show-data"],
        ["--institution", INSTITUTION],
        ["--mode", "multiuser", "--institution", "http://localhost"],
        ["--mode", "multiuser", "--institution", "https://synthetic.etlab.in/login"],
        ["--mode", "multiuser", "--institution", "https://synthetic.etlab.in:8443"],
        ["--mode", "multiuser", "--institution", "https://synthetic.etlab.in:80"],
    ],
)
def test_invalid_argument_combinations_fail_before_prompts(args, monkeypatch, capsys):
    def forbidden(_):
        pytest.fail("Invalid configuration must not prompt or request")

    monkeypatch.setattr(example, "hidden_input", forbidden)
    assert execute(forbidden, args, environ={}) == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize("url", [INSTITUTION, INSTITUTION + ":443"])
def test_institution_accepts_default_and_explicit_https_port(url):
    assert example.validate_url(url, institution=True) == url


def test_missing_token_uses_hidden_prompt_and_not_server_environment(monkeypatch, capsys):
    labels = []

    def prompt(label):
        labels.append(label)
        return OPERATOR_TOKEN

    monkeypatch.setattr(example, "hidden_input", prompt)

    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {OPERATOR_TOKEN}"
        return httpx.Response(200, json=capabilities())

    assert execute(handler, environ={"API_TOKEN": "unused-server-secret"}) == 0
    assert labels == ["Deployment API token: "]
    assert_private(capsys.readouterr())


@pytest.mark.parametrize("token", ["", "token with spaces", "token\nheader", "tökén"])
def test_invalid_operator_token_fails_without_network(token, capsys):
    def forbidden(_):
        pytest.fail("Invalid token must not enter a request")

    assert execute(forbidden, environ={"ETLAB_API_TOKEN": token}) == 2
    captured = capsys.readouterr()
    if token:
        assert token not in captured.err


def test_getpass_fallback_is_rejected_instead_of_echoing(monkeypatch, capsys):
    def fallback(_):
        warnings.warn("echoing password", example.getpass.GetPassWarning, stacklevel=1)
        pytest.fail("Echoed input must not proceed")

    monkeypatch.setattr(example.getpass, "getpass", fallback)
    assert execute(lambda _: pytest.fail("No request expected"), environ={}) == 2
    assert capsys.readouterr().err == "Hidden input requires an interactive terminal.\n"


@pytest.mark.parametrize("interruption,code", [(EOFError, 2), (KeyboardInterrupt, 130)])
def test_interrupted_prompts_exit_without_traceback(interruption, code, monkeypatch, capsys):
    def prompt(_):
        raise interruption(PASSWORD)

    monkeypatch.setattr(example, "hidden_input", prompt)
    assert execute(lambda _: pytest.fail("No request expected"), MULTIUSER) == code
    assert_private(capsys.readouterr())


@pytest.mark.parametrize("cleanup_status", [204, 503])
def test_cancellation_after_login_still_cleans_up(cleanup_status, monkeypatch, capsys):
    prompts(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(201, json=login())
        if request.method == "DELETE":
            return api_error(cleanup_status)
        raise KeyboardInterrupt(PASSWORD)

    assert execute(handler, MULTIUSER) == 130
    assert [request.method for request in calls] == ["POST", "GET", "DELETE"]
    captured = capsys.readouterr()
    assert "Cancelled." in captured.err
    assert ("Session cleanup failed" in captured.err) == (cleanup_status == 503)
    assert_private(captured)


def test_client_disables_proxies_and_redirects(monkeypatch, capsys):
    original_client = example.httpx.Client
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(example.httpx, "Client", client)
    assert execute(lambda _: httpx.Response(200, json=capabilities())) == 0
    assert options[0]["follow_redirects"] is False
    assert options[0]["trust_env"] is False
    assert options[0]["timeout"] == 60
    assert_private(capsys.readouterr())
