"""Check public HTTP serving and the unauthenticated boundary without student credentials."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base_url = args.api_url.rstrip("/")
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        for path in ("/healthz", "/docs", "/redoc", "/openapi.json", "/v1/me/profile"):
            request = Request(base_url + path)
            try:
                response = opener.open(request, timeout=15)
            except HTTPError as error:
                response = error
            with response:
                status = response.getcode()
                headers = response.headers
                body = response.read(2 * 1024 * 1024)
            expected = 401 if path == "/v1/me/profile" else 200
            if status != expected:
                print(f"FAIL {path}: expected HTTP {expected}, got {status}", file=sys.stderr)
                return 1
            if path == "/healthz" and json.loads(body) != {"status": "ok"}:
                print("FAIL /healthz: unexpected liveness response", file=sys.stderr)
                return 1
            if path == "/openapi.json":
                schema = json.loads(body)
                if not {"/v1/me/profile", "/v1/sessions", "/v1/capabilities"}.issubset(
                    schema["paths"]
                ):
                    print("FAIL /openapi.json: missing documented routes", file=sys.stderr)
                    return 1
            if path == "/v1/me/profile":
                envelope = json.loads(body)
                if (
                    envelope["error"]["code"] != "unauthorized"
                    or headers.get("Cache-Control") != "no-store"
                    or headers.get("X-Content-Type-Options") != "nosniff"
                    or not headers.get("X-Request-ID")
                    or envelope["error"]["request_id"] != headers.get("X-Request-ID")
                    or headers.get("WWW-Authenticate") != "Bearer"
                ):
                    print("FAIL /v1/me/profile: incorrect authentication boundary", file=sys.stderr)
                    return 1
            print(f"PASS {path}: HTTP {status}")
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        print("FAIL: connection or response validation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
