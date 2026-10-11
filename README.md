# ETLab API

A self-hosted, read-only API for student data in ETLab college portals. Configure an institution URL, authenticate with your student account, and read the portal through one consistent REST interface.

This project uses the web portal. It is an independent integration and is not an official ETLab service. Institution layouts and account permissions vary. **Authenticated Sahrdaya reads passed for profile, subjects, timetable, attendance, and results.** Further feature coverage and other institutions remain unverified. See the [compatibility matrix](docs/compatibility.md) and [verification record](docs/verification.md) before relying on a feature.

The API preserves the college's labels, tables, text, filters, and downloadable links. It does not invent grades, calculate attendance from incomplete records, or turn unknown table headings into assumed academic meanings. See [response formats and endpoints](docs/api.md).

## Quick start

Requires Python 3.12 or later. From this directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put the generated token in `API_TOKEN` and fill in the credentials in `.env`:

```dotenv
APP_MODE=personal
API_TOKEN=<your-generated-token>
ETLAB_BASE_URL=https://sahrdaya.etlab.in
ETLAB_USERNAME=<your-student-username>
ETLAB_PASSWORD=<your-student-password>
```

Protect the file, then start one worker:

```sh
chmod 600 .env
uvicorn etlab_api.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers
```

Alternatively, use uv:

```sh
uv sync --extra dev
uv run uvicorn etlab_api.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers
```

Open [Swagger UI](http://127.0.0.1:8000/docs) or [ReDoc](http://127.0.0.1:8000/redoc). The generated schema is at `/openapi.json`. Academic requests need `Authorization: Bearer <API_TOKEN>` in personal mode. Do not share this token: it grants access to your student data.

```sh
# Set this in the current shell without putting the token in shell history.
read -rsp 'API token: ' ETLAB_API_TOKEN; export ETLAB_API_TOKEN; printf '\n'
curl --fail-with-body -H "Authorization: Bearer $ETLAB_API_TOKEN" \
  http://127.0.0.1:8000/v1/capabilities
curl --fail-with-body -H "Authorization: Bearer $ETLAB_API_TOKEN" \
  http://127.0.0.1:8000/v1/me/attendance
```

The first authenticated request logs in to the upstream portal. Later requests reuse that session and cache each parsed result for 60 seconds by default. The token does not authenticate directly to ETLab.

## What the API exposes

The resource interface covers profile, dashboard, semesters, subjects, timetable, attendance, results, assessments, assignments, materials, notices, calendar, fees, receipts, leave, library, placement, surveys, and documents. A resource is usable only when the adapter discovers an accessible page for that account. An institution or account may expose only a subset.

- `GET /v1/capabilities` reports discovered features and verification status.
- `GET /v1/me/{resource}` returns page content and metadata.
- `GET /v1/me/{resource}/{detail_id}` follows a detail link returned by that resource.
- `GET /v1/attachments/{attachment_id}` downloads an attachment returned by a page.
- `GET /v1/session` inspects the current API session; `DELETE /v1/session` clears it.
- `POST /v1/sessions` creates an isolated student session in multi-user mode.

Academic operations are read-only. The adapter submits the login form using POST and retrieves identified academic navigation/filter forms using GET. It does not submit assignments, pay fees, apply for leave, answer surveys, or update a profile.

## Operating modes

**Personal** is the default. The server holds one student's `.env` credentials and uses `API_TOKEN` for authenticated requests. On upstream expiry it attempts one fresh login. Clearing the API session discards the current cookies/cache; the next request can log in again using the configured credentials.

**Multi-user** uses `APP_MODE=multiuser`. A trusted application backend presents `API_TOKEN` to `POST /v1/sessions` with an institution URL and the student's credentials. The response contains a random session token. Subsequent reads and session deletion use that session token. The deployment token cannot read student data. Credentials are discarded after the login attempt; an expired upstream login requires creating a new session.

Multi-user sessions expire after 30 idle minutes or 8 hours overall by default. Sessions, cookies, links, and parsed response caches are held in memory. A process restart clears them. Run **one Uvicorn worker and one application replica**; a shared session store is not implemented.

See [authentication examples](docs/api.md#authentication), [configuration](docs/configuration.md), and [deployment](docs/deployment.md).

## Documentation

- [API reference and curl/Python examples](docs/api.md)
- [Configuration and defaults](docs/configuration.md)
- [Local/Docker deployment and token rotation](docs/deployment.md)
- [Dokploy deployment without stored student credentials](docs/dokploy.md)
- [Institution compatibility and validation status](docs/compatibility.md)
- [Current verification evidence and remaining limits](docs/verification.md)
- [Adapter contribution and verification guide](docs/contributing.md)
- [Privacy and security model](docs/security.md)
- [Future MCP integration](docs/mcp.md)

## Development and verification

```sh
python -m pytest -m 'not live'
ruff check .
mypy src/etlab_api
python -m pip wheel --no-deps . -w /tmp/etlab-api-wheels
```

Tests use synthetic/sanitized HTML and mocked upstream responses. They do not prove a real institution works. The opt-in live tests read credentials from `.env`, fetch read-only pages, and validate response structure without printing or saving student data:

```sh
ETLAB_LIVE_TESTS=1 python -m pytest -m live -q
```

Fill the personal credentials and a generated `API_TOKEN` first. See the [live-testing guide](docs/contributing.md#live-verification). Do not put credentials, cookies, tokens, or unredacted portal captures in issues or pull requests.

## License

[MIT](LICENSE), copyright 2026 Midhun P M.
