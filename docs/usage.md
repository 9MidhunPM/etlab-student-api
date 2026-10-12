# Using ETLab API

This walkthrough starts with a running server and ends with reading a discovered resource and clearing a session. Follow the [README setup instructions](../README.md) first. The [API reference](api.md) contains the full endpoint, filter, and error rules; this guide explains the order to use them.

Use personal mode for your own account. Use multi-user mode when building a trusted backend that creates separate student sessions. The project does not supply frontend login, user registration, or an MCP server. See the [security model](security.md) and [future MCP guidance](mcp.md).

## Try the runnable client

From the repository root, with the project's Python environment active:

```bash
python examples/read_student.py --help
python examples/read_student.py --mode personal
python examples/read_student.py --mode personal --resource attendance
```

The [example client](../examples/read_student.py) reads `ETLAB_API_TOKEN` from your shell environment or asks for it with a hidden prompt. It does not load the server's `.env`. Its default API URL is `http://127.0.0.1:8000`; use `--api-url https://api.example.edu` for your remote server.

Without `--resource`, it lists available capabilities. With `--resource`, it checks availability first and prints metadata rather than academic values. To deliberately display the private resource JSON in your terminal:

```bash
python examples/read_student.py --resource attendance --show-data
```

`--show-data` requires `--resource`. The example keeps the personal server's shared upstream session available for subsequent reads.

For a server configured with `APP_MODE=multiuser`:

```bash
python examples/read_student.py --mode multiuser \
  --institution https://sahrdaya.etlab.in --resource attendance
```

Use your actual allowed institution origin. The client asks for the deployment token and hidden student username/password, creates a session, reads capabilities and the selected resource, then attempts to delete **the session it created** in cleanup. It does not save student credentials or print the student token. Each invocation creates a new session; links from a displayed multi-user response become unusable when that session is deleted. Use the manual workflow below when keeping one session open for filters, details, or downloads.

The example accepts HTTP only for loopback/localhost, uses HTTPS for remote APIs, and does not follow redirects or environment proxies. It makes no automatic retries. If hidden prompting is unavailable, run it from an interactive terminal; personal automation can supply `ETLAB_API_TOKEN` through its secret environment. Multi-user credential prompts are intended for an interactive demonstration, not an unattended backend login mechanism.

## Check the server

All shell examples in this guide use **Bash**. Replace the API origin for your deployment and use HTTPS remotely. In the Python snippets, change `base_url` to that same HTTPS origin when using a remote server:

```bash
ETLAB_API_URL=http://127.0.0.1:8000
curl --noproxy '*' --fail-with-body --max-time 10 "$ETLAB_API_URL/healthz"
```

Expected response:

```json
{"status": "ok"}
```

This proves the API process responds. It does not test ETLab credentials, login, parser compatibility, or student records. Public documentation is at `/docs`, `/redoc`, and `/openapi.json` when `DOCS_ENABLED=true`.

## Authenticate in Swagger

Open [local Swagger UI](http://127.0.0.1:8000/docs), or the `/docs` page on your server. Expand an endpoint, select **Try it out**, then **Execute**. Use **Authorize** to enter the token itself; Swagger adds the `Bearer` prefix. Authorization is not persisted across reloads.

| Server mode | Authorization workflow |
| --- | --- |
| Personal | Enter the configured `API_TOKEN`. Execute `GET /v1/capabilities`, then an available `GET /v1/me/{resource}` endpoint. |
| Multi-user | Enter the deployment `API_TOKEN`; execute `POST /v1/sessions` with the institution origin and student's credentials. Privately copy `session_token` from the 201 response. In **Authorize**, log out of the deployment token and enter that student token. Use it for capabilities, resource reads, inspection, and deletion. |

See the [session-creation body and response](api.md#multi-user-mode). Keep the deployment token in the operator/trusted backend: a browser-facing application must supply its own user authentication and handle session creation through that backend. A deployment token used for multi-user student reads returns 403 `session_token_required`.

In personal mode the first capabilities, academic-page, or attachment operation performs the upstream login. `/healthz`, documentation, `GET /v1/session`, and session deletion do not authenticate to ETLab. In multi-user mode `POST /v1/sessions` performs the login before returning a token.

## Prepare a manual read token

The remaining curl and Python examples use `ETLAB_AUTH_TOKEN`: your personal `API_TOKEN`, or a returned multi-user **student session token**. Keep the multi-user session active until you finish these examples. Do not use a token from an example-client run that already deleted its session.

Enter the token interactively so its literal value is not in shell history:

```bash
read -r -s -p 'Read/session bearer token: ' ETLAB_AUTH_TOKEN
export ETLAB_AUTH_TOKEN
printf '\n'
```

These commands send the token only in the Authorization header. Do not enable shell tracing while handling secrets. Resource curl examples intentionally print private academic JSON in your own terminal; use the runnable client's default output when only checking metadata.

## Discover available resources

```bash
curl --noproxy '*' --fail-with-body --max-time 60 \
  -H "Authorization: Bearer $ETLAB_AUTH_TOKEN" \
  "$ETLAB_API_URL/v1/capabilities"
```

This is a **synthetic, shortened** example. The current adapter returns an entry for all 19 resources:

```json
{
  "institution": "https://sahrdaya.etlab.in",
  "adapter": "generic-student-html",
  "adapter_version": "0.1.0",
  "verification": "fixture_tested",
  "features": [
    {"resource": "attendance", "available": true, "status": "discovered", "reason": null},
    {
      "resource": "library",
      "available": false,
      "status": "unavailable",
      "reason": "No reviewed student read route was discovered in this session."
    }
  ]
}
```

Choose a resource with `available=true`. That means an accessible-looking read route was discovered for this account; permissions or layout can still fail when it is read. Top-level `fixture_tested` describes adapter test coverage. Neither value proves a current live account's details, filters, or files have been verified. Use the [compatibility matrix](compatibility.md) for recorded institution evidence.

## Read and interpret a resource

If attendance is available:

```bash
curl --noproxy '*' --fail-with-body --max-time 60 \
  -H "Authorization: Bearer $ETLAB_AUTH_TOKEN" \
  "$ETLAB_API_URL/v1/me/attendance"
```

An academic response has `data` and `meta`. See the [synthetic populated response](api.md#academic-response) for the complete shape.

| Inspect | How to use it |
| --- | --- |
| `data.fields` | Read the portal's label/value pairs; values are strings. |
| `data.tables` | Use each table's ordered `columns` and corresponding row keys. Preserve the institution's labels rather than assuming a column's academic meaning. |
| `data.text` | Read page notices and paragraphs that are not table rows. |
| `data.filters`, `data.links` | Find the controls, detail IDs, and attachment IDs supported by this page/session. |
| `meta.fetched_at`, `meta.cache_hit` | Distinguish the time of the upstream fetch from reuse of its cached result. |
| `meta.total_rows` | Sum of rows across all tables before local pagination; it is not an attendance/grade total. |
| `data.empty` | Recognized portal indication that there are no records. |

This **synthetic** successful empty response is different from an error:

```json
{
  "data": {
    "title": "Assignments",
    "fields": {},
    "tables": [],
    "text": ["No records found."],
    "links": [],
    "filters": [],
    "empty": true
  },
  "meta": {
    "institution": "https://sahrdaya.etlab.in",
    "fetched_at": "2026-10-12T06:30:00Z",
    "cache_hit": false,
    "page": 1,
    "page_size": 100,
    "total_rows": 0
  }
}
```

Do not turn 401/403 errors, `unsupported_resource`, or `unsupported_layout` into an empty collection. A page beyond the available rows can have empty row lists while `data.empty=false`.

## Apply returned filters and local pagination

First read the unfiltered page and inspect `data.filters`. Use only an exact returned `name` with `supported=true`; for a select, choose an advertised option's **value**, not its display label. Input controls have their own accepted formats and limits in the [filter reference](api.md#pagination-and-filtering).

This example is **illustrative** and works only if your response offers `Attendance[semester]` with option value `3`:

```bash
curl --noproxy '*' --fail-with-body --max-time 60 --get \
  -H "Authorization: Bearer $ETLAB_AUTH_TOKEN" \
  --data-urlencode 'Attendance[semester]=3' \
  --data-urlencode 'page=1' \
  --data-urlencode 'page_size=50' \
  "$ETLAB_API_URL/v1/me/attendance"
```

There are no universal `semester`, `date_from`, or `date_to` aliases. Multiple filters must belong to the same supported read-only GET form. Brackets and other special characters should be URL-encoded; `curl --data-urlencode` and HTTPX's `params` do this for you.

Equivalent Python, using the token exported above and replacing the illustrative filter name/value with your returned controls:

```python
import os
import httpx

with httpx.Client(
    base_url="http://127.0.0.1:8000",
    headers={"Authorization": f"Bearer {os.environ['ETLAB_AUTH_TOKEN']}"},
    timeout=60,
    follow_redirects=False,
    trust_env=False,
) as client:
    response = client.get(
        "/v1/me/attendance",
        params={"Attendance[semester]": "3", "page": 1, "page_size": 50},
    )
    response.raise_for_status()
    academic = response.json()
    print("Fetched:", academic["meta"]["fetched_at"])
    print("Rows before pagination:", academic["meta"]["total_rows"])
```

`page` defaults to 1; `page_size` defaults to 100 and accepts 1–500. Each table is paginated independently with the same values. Page 2 means a different local slice of the parsed tables, not a request for the portal's second page. The default 60-second cache keeps the original fetch timestamp on hits; changing local page/page-size can reuse that cache. It is an on-demand cache, not a scheduled academic archive.

## Follow details and download files

Read the resource's `data.links`. For `kind="detail"`, use `/v1/me/{resource}/{id}` with the resource that returned it. For `kind="attachment"`, use `/v1/attachments/{id}`. IDs are opaque and session-owned; they are not URLs. A detail ID from assignments is not an attendance ID.

Replace this placeholder with an actual detail ID from the active session:

```bash
curl --noproxy '*' --fail-with-body --max-time 60 \
  -H "Authorization: Bearer $ETLAB_AUTH_TOKEN" \
  "$ETLAB_API_URL/v1/me/assignments/<returned-detail-id>"
```

A detail response has the same `data`/`meta` shape and can expose more read links. Deleted/expired sessions, process restarts, or registry eviction make old IDs unusable. Rediscover them with the active session; the [API reference](api.md#detail-links-and-files) explains cache/descriptor lifetimes.

For a file, verify HTTP success **before opening the destination**. The following example intentionally displays private link labels and saves the selected attachment. Use a resource that capabilities advertises and that actually returns attachments; `materials` is only an example. It uses the active token from the manual workflow, and refuses to overwrite `downloaded-material.bin`.

```python
import os
from pathlib import Path
import httpx

resource = "materials"
with httpx.Client(
    base_url="http://127.0.0.1:8000",
    headers={"Authorization": f"Bearer {os.environ['ETLAB_AUTH_TOKEN']}"},
    timeout=60,
    follow_redirects=False,
    trust_env=False,
) as client:
    capabilities = client.get("/v1/capabilities")
    capabilities.raise_for_status()
    available = {item["resource"] for item in capabilities.json()["features"] if item["available"]}
    if resource not in available:
        raise SystemExit("Choose a resource advertised as available.")
    response = client.get(f"/v1/me/{resource}")
    response.raise_for_status()
    attachments = [
        link for link in response.json()["data"]["links"] if link["kind"] == "attachment"
    ]
    if not attachments:
        raise SystemExit("This page returned no attachment links.")
    for number, link in enumerate(attachments, start=1):
        print(number, link["label"])
    choice = input("Attachment number to save: ")
    if not choice.isdecimal() or not 1 <= int(choice) <= len(attachments):
        raise SystemExit("Choose one of the listed attachment numbers.")
    attachment_id = attachments[int(choice) - 1]["id"]
    downloaded = client.get(f"/v1/attachments/{attachment_id}")
    downloaded.raise_for_status()
    with Path("downloaded-material.bin").open("xb") as destination:
        destination.write(downloaded.content)
    print("Saved bytes:", len(downloaded.content))
```

No file is created on an HTTP error. The server bounds file size and rejects portal HTML login/error pages; the default limit is 25 MiB. The server does not save or cache attachment bodies. Your explicitly saved client file persists until you remove it. Downloads from another host require the operator's attachment-host allowlist and a discovered safe link.

## Inspect and clear the session

```bash
curl --noproxy '*' --fail-with-body --max-time 10 \
  -H "Authorization: Bearer $ETLAB_AUTH_TOKEN" \
  "$ETLAB_API_URL/v1/session"
```

Synthetic personal-mode response:

```json
{"mode": "personal", "institution": "https://sahrdaya.etlab.in", "expires_at": null}
```

Synthetic multi-user response:

```json
{
  "mode": "multiuser",
  "institution": "https://sahrdaya.etlab.in",
  "expires_at": "2026-10-12T07:00:00Z"
}
```

For multi-user sessions, successful token authentication renews the idle clock; inspection counts as activity. The returned deadline is the earlier of the idle deadline and maximum lifetime. Default limits are 30 idle minutes and 8 hours overall. Inspection does not check the upstream login, and polling cannot extend the maximum lifetime. See the [session lifecycle](api.md#session-lifecycle).

Finish the manual workflow with deletion:

```bash
curl --noproxy '*' --fail-with-body --max-time 10 \
  --request DELETE --write-out '\nHTTP %{http_code}\n' \
  -H "Authorization: Bearer $ETLAB_AUTH_TOKEN" \
  "$ETLAB_API_URL/v1/session"
unset ETLAB_AUTH_TOKEN
```

Success is HTTP 204 with no JSON body. Multi-user deletion revokes that student token and clears its cookies, cache, and links. Personal deletion clears the shared upstream state; its configured `API_TOKEN` still works, and the next capabilities/academic operation can log in again. A 401 during cleanup means the student token is already invalid or expired. A server restart clears all multi-user sessions.

## Handle failures in a client

Check the HTTP status before interpreting academic data or saving bytes. Errors return `error.code`, `error.message`, and `error.request_id`; responses also carry `X-Request-ID`. Keep the code and request ID for diagnosis without copying credentials or student payloads into logs/issues.

For an invalid token, correct the personal token or create a new multi-user session through your backend. For `auth_expired`, personal mode already tried one reauthentication; multi-user mode needs a new session. Correct invalid credentials and unsupported filters instead of retrying them. Respect `Retry-After` on login throttling; retry temporary read failures conservatively with backoff rather than bulk polling. Do not blindly replay login POSTs after a timeout because the server may have created a session even if its response was lost.

Use the [error reference](api.md#errors-and-retry-behavior) and [troubleshooting guide](troubleshooting.md) to distinguish token, upstream, compatibility, and setup failures. Example-client success establishes only what that invocation read; it does not certify another institution or unvisited filters/details/downloads.
