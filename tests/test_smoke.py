"""Public smoke checks must work through proxies rejecting urllib's generic identity."""

import importlib.util
import io
import json
import sys
from email.message import Message
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "smoke_api.py"
SPEC = importlib.util.spec_from_file_location("smoke_api", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class Response(io.BytesIO):
    def __init__(self, status, body, headers=None):
        super().__init__(json.dumps(body).encode())
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def getcode(self):
        return self.status


def test_public_checks_identify_project_without_authorization(monkeypatch, capsys):
    paths = []

    class EdgeProxy:
        def open(self, request, timeout):
            assert not request.has_header("Authorization")
            assert timeout == 15
            user_agent = request.get_header("User-agent", "Python-urllib/3.12")
            if user_agent.startswith("Python-urllib"):
                return Response(403, "Blocked default client identity")
            path = request.full_url.removeprefix("https://api.example.edu")
            paths.append(path)
            if path == "/healthz":
                return Response(200, {"status": "ok"})
            if path == "/openapi.json":
                return Response(
                    200,
                    {
                        "paths": {
                            name: {}
                            for name in ("/v1/me/profile", "/v1/sessions", "/v1/capabilities")
                        }
                    },
                )
            if path == "/v1/me/profile":
                return Response(
                    401,
                    {"error": {"code": "unauthorized", "request_id": "synthetic-request-id"}},
                    {
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                        "X-Request-ID": "synthetic-request-id",
                        "WWW-Authenticate": "Bearer",
                    },
                )
            return Response(200, "Documentation")

    monkeypatch.setattr(smoke, "build_opener", lambda *handlers: EdgeProxy())
    monkeypatch.setattr(sys, "argv", ["smoke_api.py", "--api-url", "https://api.example.edu"])
    assert smoke.main() == 0
    assert paths == ["/healthz", "/docs", "/redoc", "/openapi.json", "/v1/me/profile"]
    assert capsys.readouterr().err == ""
