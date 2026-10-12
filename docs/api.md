# API reference

Base URL for local development: `http://127.0.0.1:8000`. Use HTTPS for remote deployments. All data/session endpoints require a bearer token. `/healthz` and enabled documentation/schema routes are public.

For your first request, follow the [usage walkthrough](usage.md). This page describes the complete response contract and endpoint rules. Shell examples use **Bash** in an interactive terminal; resource responses can contain private student data.

Interactive documentation lives at `/docs` and `/redoc`; the schema is `/openapi.json`. The schema contains a concrete list and detail route for each resource. Dynamic upstream filter names cannot be enumerated statically; inspect the returned `data.filters` collection, including each control's `input_type`.

## Authentication

Send `Authorization: Bearer <token>`. Tokens are not accepted through query parameters. Swagger's Authorize dialog accepts the token; authorization persistence is disabled.

The adapter preserves observed hidden login fields and the selected Login submit control's discovered name/value. A named button without a value contributes an empty string, as in Sahrdaya's observed `yt0=` control. If several enabled submit controls exist, an unambiguous Login/Log in/Sign in choice is required. Ambiguous controls, field conflicts, and unsafe form destination/method overrides fail before credentials are submitted.

### Personal mode

Use the configured `API_TOKEN` for reads and session inspection/deletion. Server-side `.env` credentials perform the upstream login lazily on the first capabilities, academic-page, or attachment operation. Health checks, documentation, session inspection, and deletion do not log in to the portal. One reauthentication is attempted when the portal session expires.

```bash
read -r -s -p 'API token: ' ETLAB_API_TOKEN
export ETLAB_API_TOKEN
printf '\n'
curl --noproxy '*' --fail-with-body --max-time 60 -H "Authorization: Bearer $ETLAB_API_TOKEN" \
  http://127.0.0.1:8000/v1/me/profile
```

Python:

```python
import os
import httpx

with httpx.Client(
    base_url="http://127.0.0.1:8000",
    headers={"Authorization": f"Bearer {os.environ['ETLAB_API_TOKEN']}"},
    timeout=60,
    follow_redirects=False,
    trust_env=False,
) as client:
    response = client.get("/v1/me/attendance", params={"page": 1, "page_size": 50})
    response.raise_for_status()
    attendance = response.json()
    print(attendance["meta"]["fetched_at"], attendance["meta"]["total_rows"])
```

### Multi-user mode

Configure `APP_MODE=multiuser`. Only a trusted backend should possess the deployment `API_TOKEN`.

`POST /v1/sessions` uses that deployment token and this JSON body:

```json
{
  "base_url": "https://sahrdaya.etlab.in",
  "username": "<student-username>",
  "password": "<student-password>"
}
```

On success, HTTP 201 returns:

```json
{
  "session_token": "<opaque-secret-token>",
  "expires_at": "2026-10-11T20:00:00Z",
  "idle_timeout_seconds": 1800,
  "institution": "https://sahrdaya.etlab.in"
}
```

`expires_at` is the maximum lifetime. The session can expire sooner through idleness, upstream authentication expiry, deletion, or restart. Use the returned student token for subsequent requests. Using the deployment token for student reads returns `session_token_required`.

The following curl example reads credentials interactively and streams the JSON body on stdin. It stores the session token without printing it. Run the complete block in a fresh Bash shell; its exit trap attempts cleanup if a later request fails. For capability checks, selected resource reads, and safe diagnostics, the [runnable client](../examples/read_student.py) is the simpler option.

```bash
set -euo pipefail
read -r -s -p 'Deployment API token: ' ETLAB_API_TOKEN
export ETLAB_API_TOKEN
printf '\n'
ETLAB_SESSION_TOKEN="$(
  python -c 'import getpass,json; print(json.dumps({"base_url":"https://sahrdaya.etlab.in","username":getpass.getpass("Student username: "),"password":getpass.getpass("Student password: ")}))' |
  curl --noproxy '*' --silent --show-error --fail-with-body --max-time 60 \
    -H "Authorization: Bearer $ETLAB_API_TOKEN" \
    -H 'Content-Type: application/json' \
    --data-binary @- http://127.0.0.1:8000/v1/sessions |
  python -c 'import json,sys; print(json.load(sys.stdin)["session_token"])'
)"
export ETLAB_SESSION_TOKEN
cleanup_session() {
  curl --noproxy '*' --silent --show-error --max-time 5 --output /dev/null \
    -X DELETE -H "Authorization: Bearer $ETLAB_SESSION_TOKEN" \
    http://127.0.0.1:8000/v1/session || true
  unset ETLAB_SESSION_TOKEN
}
trap cleanup_session EXIT
curl --noproxy '*' --fail-with-body --max-time 60 \
  -H "Authorization: Bearer $ETLAB_SESSION_TOKEN" \
  http://127.0.0.1:8000/v1/capabilities
cleanup_session
trap - EXIT
unset ETLAB_API_TOKEN
```

Python, including cleanup:

```python
import getpass
import os
import httpx

with httpx.Client(
    base_url="http://127.0.0.1:8000",
    timeout=60,
    follow_redirects=False,
    trust_env=False,
) as client:
    login = client.post(
        "/v1/sessions",
        headers={"Authorization": f"Bearer {os.environ['ETLAB_API_TOKEN']}"},
        json={
            "base_url": "https://sahrdaya.etlab.in",
            "username": getpass.getpass("Student username: "),
            "password": getpass.getpass("Student password: "),
        },
    )
    login.raise_for_status()
    client.headers["Authorization"] = f"Bearer {login.json()['session_token']}"
    try:
        response = client.get("/v1/capabilities")
        response.raise_for_status()
        available = [item["resource"] for item in response.json()["features"] if item["available"]]
        print("Available modules:", ", ".join(available))
    finally:
        try:
            deleted = client.delete("/v1/session", timeout=5)
            if deleted.status_code not in (204, 401):
                print("Session cleanup failed; HTTP", deleted.status_code)
        except httpx.RequestError:
            print("Session cleanup could not reach the API.")
```

Never print or log login payloads or bearer tokens. Multi-user passwords are discarded after login. The upstream session cannot automatically reauthenticate; create a new API session when it expires.

Cleanup is best effort when the server cannot be reached. A session whose token was not received after a failed login response cannot be deleted by the client and expires according to the server's limits. Do not blindly repeat session-creation POSTs after transport failures. See [errors and retry behavior](#errors-and-retry-behavior).

## Endpoints

| Method and path | Response | Purpose |
| --- | --- | --- |
| `GET /healthz` | `{"status":"ok"}` | Worker liveness only; does not contact ETLab. |
| `POST /v1/sessions` | `SessionCreated`, HTTP 201 | Multi-user login with deployment token. Personal mode returns `mode_mismatch`. |
| `GET /v1/session` | `SessionInfo` | Mode, institution, and effective expiry; personal expiry is `null`. |
| `DELETE /v1/session` | HTTP 204, no body | Clear upstream cookies, cache, and links. Revoke a multi-user student token; the personal deployment token remains valid. |
| `GET /v1/capabilities` | `Capabilities` | Account-specific module discovery and verification status. |
| `GET /v1/me/{resource}` | `AcademicResponse` | Retrieve an academic page with supported filters and local pagination. |
| `GET /v1/me/{resource}/{detail_id}` | `AcademicResponse` | Follow a detail ID returned by this resource and session. |
| `GET /v1/attachments/{attachment_id}` | Bytes | Download an attachment ID returned by this session. |

Resource names:

| Resource | Intended portal content, if exposed |
| --- | --- |
| `profile`, `dashboard` | Student profile and portal home content. |
| `semesters`, `subjects`, `timetable` | Semester/subject listings and timetable. |
| `attendance` | Attendance summaries and linked details. |
| `results`, `assessments` | Published results, internal assessments, and linked details. |
| `assignments`, `materials` | Assignment records/submission status and study material files. |
| `notices`, `calendar` | Notices and academic calendar content. |
| `fees`, `receipts` | Fee/dues information and existing receipts. |
| `leave`, `library` | Existing leave status and library information. |
| `placement`, `surveys`, `documents` | Read-only placement/survey listings and academic document links. |

No resource name guarantees an institution has that module. Use capabilities first. Rich semantic grade/attendance models and cross-college inferred totals are not provided; the table schema retains the portal's own labels.

## Academic response

The following example is synthetic. Labels and option names are illustrative, not verified Sahrdaya output:

```json
{
  "data": {
    "title": "Attendance",
    "fields": {"Semester": "Semester 3"},
    "tables": [{
      "caption": null,
      "columns": ["Subject", "Present", "Percentage"],
      "rows": [{"Subject": "Example subject", "Present": "18", "Percentage": "90%"}]
    }],
    "text": [],
    "links": [{"id": "opaque-id", "label": "Details", "kind": "detail"}],
    "filters": [{
      "name": "Attendance[semester]",
      "label": "Semester",
      "input_type": "select",
      "options": [{"value": "3", "label": "Semester 3"}],
      "current": "3",
      "supported": true
    }],
    "empty": false
  },
  "meta": {
    "institution": "https://sahrdaya.etlab.in",
    "fetched_at": "2026-10-11T12:00:00Z",
    "cache_hit": false,
    "page": 1,
    "page_size": 100,
    "total_rows": 1
  }
}
```

| Field | Meaning |
| --- | --- |
| `data.title` | Visible page heading/title. |
| `data.fields` | Label/value pairs such as profile fields. Values remain strings; repeated labels receive collision-free numeric suffixes so values are preserved. |
| `data.tables[].caption` | Optional displayed table caption. |
| `data.tables[].columns` | Ordered headings; merged headers are joined, duplicate headings disambiguated, absent headings assigned `Column N`. |
| `data.tables[].rows` | Dictionaries keyed by headings, with displayed string values. |
| `data.text` | Extracted notices/paragraph text with whitespace normalized. |
| `data.links` | Safe detail/file links represented by opaque `id`, visible `label`, and `kind` (`detail` or `attachment`). |
| `data.filters` | Discovered filter controls with exact names, labels, input type, options, current value, and safe-submission support. |
| `data.empty` | Portal explicitly indicated no records. It is not inferred solely from missing tables or local pagination. |
| `meta.institution` | Upstream college origin. |
| `meta.fetched_at` | UTC upstream fetch timestamp; unchanged on a cache hit. |
| `meta.cache_hit` | Result reused from this session's parsed-response cache. |
| `meta.page`, `meta.page_size` | Applied local pagination. |
| `meta.total_rows` | Sum of all table row counts before local pagination. |

Fields, text, filters, and links are retained on each local page. Every table is paginated independently using the same page/page-size; `total_rows` is a sum, not the row count of one table. A page beyond the available table rows can contain empty row lists while `data.empty` remains false.

### Pagination and filtering

`page` defaults to 1 and must be at least 1. `page_size` defaults to 100 and accepts 1–500. Pagination is local to the parsed response, does not claim upstream server pagination, and reuses the same cache entry across page requests.

All other query keys are upstream filter names. First fetch the page, inspect `data.filters`, then send only a supported filter's exact `name` and an accepted value. Names may include brackets. There are no universal `semester`, `date_from`, or `date_to` aliases; semester and date-range filtering are available only when the actual upstream page advertises matching supported controls.

`input_type` is `select`, `text`, `search`, `date`, `datetime-local`, or `number`. Select controls advertise offered `{value,label}` options; input controls have an empty `options` list and expose their current string value. Their accepted formats are:

| Input type | Accepted value |
| --- | --- |
| `select` | Exactly one advertised option value. |
| `text`, `search` | Text without control characters, up to 512 characters or the form's tighter `maxlength`. |
| `date` | Valid `YYYY-MM-DD`, within any declared `min`/`max`. |
| `datetime-local` | Valid local timestamp such as `2026-09-01T09:30`, optionally with seconds/fractional seconds; no timezone suffix; within declared bounds. |
| `number` | Finite decimal number, including exponent notation, within declared bounds. |

The 512-character/tighter-`maxlength` limit and control-character rejection apply to all input controls. Empty input values are accepted to clear a filter; the portal determines their meaning. The API validates individual date inputs and preserves the form's other current/default values, while the portal determines date-range semantics. Password, credential, action, disabled, and read-only controls are not offered as editable API filters.

```bash
# Illustrative only: replace the name/value with those actually returned by data.filters.
curl --noproxy '*' --fail-with-body --max-time 60 --get \
  -H "Authorization: Bearer $ETLAB_API_TOKEN" \
  --data-urlencode 'Attendance[semester]=3' \
  --data-urlencode 'page=1' --data-urlencode 'page_size=50' \
  http://127.0.0.1:8000/v1/me/attendance
```

Multiple filters must belong to the same identified read-only GET form. Unknown names, invalid option/input values, POST filter forms, and other unsupported forms return `invalid_filter`. Duplicate query filter names return `invalid_filters`. The API admission limits are 32 filters, 255 characters per name, and 1024 characters per value; the tighter input rules above also apply. Do not send both a caller-supplied path and an upstream URL; routes use only discovered resources and IDs.

A discovered date range can be requested the same way. This example is synthetic and works only when the page actually advertises these names as supported date controls:

```bash
curl --noproxy '*' --fail-with-body --max-time 60 --get \
  -H "Authorization: Bearer $ETLAB_API_TOKEN" \
  --data-urlencode 'Attendance[from]=2026-09-01' \
  --data-urlencode 'Attendance[to]=2026-09-30' \
  http://127.0.0.1:8000/v1/me/attendance
```

### Detail links and files

A link with `kind=detail` is read at `/v1/me/{resource}/{id}` using the resource that returned it. A link with `kind=attachment` is downloaded at `/v1/attachments/{id}`. IDs are session-owned and become unusable after the API session is deleted/expires or the process restarts. They are not upstream URLs and cannot be reused across users or institutions.

Each adapter keeps at most 1,024 detail/attachment descriptors shared across resources, evicting those least recently registered or used. Parsed pages emit only IDs still retained in that registry, so an oversized page exposes at most 1,024 links. An evicted ID returns `not_found` (404); refresh its source page after the cache TTL or create a fresh API session to obtain current IDs. Personal-mode automatic reauthentication to the same account retains current descriptors for the one retry. Successful login as a different username and session destruction clear them. Descriptor retention does not guarantee an upstream file URL remains valid indefinitely.

```bash
curl --noproxy '*' --fail-with-body --max-time 60 -H "Authorization: Bearer $ETLAB_API_TOKEN" \
  http://127.0.0.1:8000/v1/me/assignments/opaque-detail-id
```

For a download, check the HTTP status **before creating the output file**. This personal-mode example deliberately saves private content. Replace the ID with an attachment returned by your active session; in multi-user mode use its student session token instead. `xb` refuses to overwrite an existing file.

```python
import os
from pathlib import Path
import httpx

with httpx.Client(
    base_url="http://127.0.0.1:8000",
    headers={"Authorization": f"Bearer {os.environ['ETLAB_API_TOKEN']}"},
    timeout=60,
    follow_redirects=False,
    trust_env=False,
) as client:
    downloaded = client.get("/v1/attachments/<id-from-data.links>")
    downloaded.raise_for_status()
    with Path("material.bin").open("xb") as destination:
        destination.write(downloaded.content)
```

`curl --fail-with-body --output material.bin` can write an error JSON body into that filename on failure. Do not treat a file's existence as a successful download. The [usage guide](usage.md#follow-details-and-download-files) shows how to select links from a resource response.

File responses include a sanitized `Content-Disposition: attachment` filename and an upstream content type when valid. Content is bounded in memory; it is not cached or stored on disk by the server. The default file limit is 25 MiB. A portal HTML login/error page is rejected rather than returned as a successful file.

Approved external file links can be registered only when discovered on a read page, their hostname is explicitly included in `ETLAB_ATTACHMENT_HOSTS`, and they have a recognized file extension. External links are checked for unsafe query/action patterns and public HTTPS destination policy. Institutional cookies are never sent to those hosts. Allowlisting a host does not create an arbitrary-URL download endpoint.

## Capabilities

Capabilities return `institution`, `adapter`, `adapter_version`, `verification`, and `features`. Each feature has `resource`, `available`, `status`, and optional `reason`. A discovered navigation link receives `status=discovered`; it does not establish that all details and filters have been tested. See the [compatibility matrix](compatibility.md) for evidence-level meanings.

The current generic adapter returns top-level `verification=fixture_tested` and one entry for every resource, with feature status `discovered` or `unavailable`. These values describe adapter fixture coverage and current navigation discovery; they do not automatically become `live_verified` after a successful request. See a [synthetic capability example](usage.md#discover-available-resources).

## Session lifecycle

`GET /v1/session` reports the authenticated API session without contacting ETLab. Personal mode returns `expires_at=null`; the configured token remains usable after clearing the upstream session. The next capabilities or academic operation can log in again from server configuration.

For multi-user mode, successful bearer authentication renews the idle clock, including session inspection and requests that subsequently fail during a resource operation. Inspection returns the earlier of the renewed idle deadline and the fixed maximum lifetime. Polling cannot extend that maximum. The session may also become unusable when upstream authentication expires, it is deleted, or the process restarts.

`DELETE /v1/session` returns 204 without JSON on success. An already invalid/expired token returns 401; cleanup clients may treat that as no active session remaining. Deletion clears the session's discovered IDs, so rediscover links after obtaining a new session. See [inspection and deletion examples](usage.md#inspect-and-clear-the-session).

## Errors and retry behavior

Errors share a safe envelope:

```json
{
  "error": {
    "code": "unsupported_resource",
    "message": "This resource was not discovered for this student account.",
    "request_id": "opaque-request-id"
  }
}
```

Every response includes `X-Request-ID`, `Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`. Authentication failures include `WWW-Authenticate: Bearer`. Login throttling returns `Retry-After: 60`.

| HTTP | Code | Client action |
| --- | --- | --- |
| 401 | `unauthorized` | Supply the correct deployment or unexpired student token. |
| 401 | `invalid_credentials` | Correct the upstream credentials; do not repeatedly retry. |
| 401 | `auth_expired` | Personal mode already attempted one reauthentication; multi-user mode must create a new session. |
| 403 | `session_token_required` | Use a student's session token for multi-user reads. |
| 403 | `upstream_denied` | Portal denied access; check account permissions. |
| 404 | `unsupported_resource` | Check capabilities; this module was not discovered/supported. |
| 404 | `not_found`, `upstream_not_found` | Unknown/stale session-owned ID or missing upstream page. |
| 409 | `mode_mismatch` | Session creation requires multi-user mode. |
| 409 | `authentication_challenge` | Portal requires an interactive challenge; no bypass is attempted. |
| 413 | `response_too_large` | Requested page/file exceeds the configured decoded size bound. |
| 422 | `invalid_request` | Invalid JSON/body/parameter shape; submitted secrets are not echoed. |
| 422 | `invalid_filter`, `invalid_filters` | Use exact supported filter controls and valid values; remove duplicates. |
| 422 | `invalid_institution`, `destination_denied` | Use an allowed public HTTPS institution/file destination. |
| 429 | `login_rate_limited` | Wait at least the returned retry interval. |
| 502 | `unsupported_layout`, `unsupported_encoding`, `upstream_error` | Portal content/encoding/layout is unsupported; report safe diagnostics rather than treating it as empty data. |
| 503 | `session_capacity` | Server session limit reached; delete unused sessions or retry later. |
| 503 | `upstream_unavailable`, `upstream_rate_limited` | Temporary upstream failure; retry with backoff. |
| 504 | `upstream_timeout` | Upstream operation timed out; retry conservatively. |
| 500 | `internal_error` | Report the request ID and version; no private error details are returned. |

Unknown HTTP routes/methods use `http_error` with their corresponding status. Adapter operations outside declared reads/login use `operation_denied` (405). Some upstream content failures can return 502 under `upstream_unavailable`; distinguish the error code and status instead of assuming one status covers every portal failure.

A recognized empty portal page is a successful `AcademicResponse` with `empty=true`. Missing permissions, unsupported modules, login pages, and layout/parser failures are errors. Do not convert these errors into an empty attendance/results collection in your client.

Pages and files request identity encoding. If an upstream server ignores that and returns compressed content, the API reports `unsupported_encoding` to preserve bounded response memory. JSON scraping semantics and unsupported layout extensions require an adapter change; they are not guessed from arbitrary responses.
