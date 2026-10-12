# Troubleshooting

Start with the failing layer: installation, configuration, local HTTP, API authorization, or the upstream portal. A passing `/healthz` only confirms the worker is running. It does not authenticate to ETLab.

Use the [setup guide](getting-started.md), [first-request walkthrough](usage.md), and [API error reference](api.md#errors-and-retry-behavior) alongside this guide. The [documentation index](README.md) links the complete set.

## Startup and local tools

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Installation rejects your Python version | The package requires Python 3.12 or newer. | Check `python --version` inside the activated virtual environment. Create the environment with a compatible interpreter; CI exercises Python 3.12 and 3.13. Follow the complete pip or uv path in [getting started](getting-started.md). |
| `etlab-api: command not found` or `No module named etlab_api` | The package was not installed into the interpreter/environment you are using. | Activate the project's virtual environment and install the package there. `python -m etlab_api.cli --help` is an equivalent entry point once the package is installed. |
| Creating a virtual environment fails because `venv` or `ensurepip` is unavailable | Your operating system's Python installation separates its virtual-environment support. | Install the distribution's venv support for your selected Python version, or use the documented uv setup path. |
| `etlab-api check` reports invalid configuration | A required token/credential is missing, the token is too short, the mode is invalid, or a numeric/hostname constraint fails. | Edit the exact setting names in your private `.env`, then rerun `check`. Personal mode needs both student credentials; multi-user mode needs an operator token and can leave credentials empty. Use [configuration](configuration.md) for defaults and constraints. |
| Settings look correct in `.env` but checks or requests disagree | The process starts in another working directory, an exported variable overrides the file, or the running process has older settings. | Launch from the configuration directory and check for shell/service-manager overrides without printing their values. Restart Python or recreate the Compose container after edits. |
| `Address already in use` or Compose cannot bind port 8000 | Another process already owns the host port. | Choose an unused port with `etlab-api serve --port 8001`. For Compose, change only the host side of `127.0.0.1:8000:8000` to an unused port, such as `127.0.0.1:8001:8000`, and recreate the service. Update your client base URL. |
| curl reports connection refused | No server is listening at that address/port, or it exited. | Check the foreground process or `docker compose ps`; inspect startup errors. Use the same port and host as the server command. |
| `/docs` or `/openapi.json` returns 404 | `DOCS_ENABLED=false`, or the proxy is routing to a different service. | Check the intended setting and base URL. Documentation is deliberately unavailable when disabled. `/healthz` remains available. |
| Swagger loads but interactive requests fail | The token is missing/wrong, or the browser cannot reach the configured API origin. | Use Swagger's Authorize dialog with the token required by your mode. Check the HTTP status and error code. Authorization is not persisted across reloads. |
| A browser frontend fails while backend/curl requests work | Browser-origin access is not configured. | Use a trusted backend integration when possible. If your frontend design requires direct browser requests, configure its exact origin in `CORS_ORIGINS` and restart/recreate; CORS does not replace user authentication. |

`check` is a configuration check: it makes no DNS lookup or upstream connection. Its success is compatible with wrong student credentials, a portal outage, or an unsupported institution layout.

## Docker and hosted routing

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `docker compose` is unavailable, or `--wait` is unknown | Compose v2 is missing or outdated. | Install/update the Compose plugin or Docker Desktop. Confirm `docker compose version` and the flags in `docker compose up --help`. |
| Docker cannot connect to its daemon or reports permission denied | Docker is stopped, your selected context is wrong, or your account lacks access. | Start Docker and check `docker version` and your intended Docker context/account permissions. |
| Image builds, but the service exits or never becomes healthy | Runtime configuration is incomplete or the process crashes. | Run `docker compose run --rm --no-deps etlab-api etlab-api check`, inspect `docker compose ps` and `docker compose logs --tail=100 etlab-api`, then correct the reported settings. A blank personal template cannot boot. |
| Changed `.env` has no effect after `docker compose restart` | Restart retained the old container's environment. | Validate the edited configuration, then run `docker compose up --force-recreate --wait --wait-timeout 120`. See [configuration reload](deployment.md#reload-configuration-and-update). |
| Local health works but public HTTPS fails | DNS, certificate, reverse-proxy target, or container-network routing is wrong. | Confirm the intended domain and route to container port 8000. A proxy container needs `etlab-api:8000` on a shared private network; its own `127.0.0.1` points back to itself. Use the [deployment](deployment.md#tls-reverse-proxy) or [Dokploy](dokploy.md#domain-and-https) guide. |
| All student tokens stop working after deployment or restart | Session state was held in the replaced process. | Create fresh student sessions through the trusted backend. Run one worker and one replica; additional processes cannot share tokens. |
| Public health works with curl but Python gets a plain 403 without the API error envelope | An edge proxy may reject the client's generic user-agent before the request reaches the API. | Use the current `scripts/smoke_api.py`, which identifies itself as `etlab-api-smoke/0.1.0`. The hosted edge was observed rejecting `Python-urllib/3.12` while accepting this project identity. If another client fails, inspect the edge policy and response headers; changing a bearer token cannot resolve a pre-API rejection. |

Do not publish full `docker compose config`, `docker inspect`, environment dumps, or secret-file contents as diagnostics. The CLI `check`, container status, and reviewed startup logs are more useful starting points.

## Authentication and session tokens

| Error or symptom | Meaning | Action |
| --- | --- | --- |
| 401 `unauthorized` | Missing/incorrect operator token in personal mode or session creation; missing, expired, or deleted student token in multi-user reads. | Send the bearer token in `Authorization`. Confirm which operation you are making. Obtain a new student session when its token has expired; check operator-token rotation if admission stopped working. |
| 403 `session_token_required` | The multi-user operator token was used for a student read. | Use `API_TOKEN` only for `POST /v1/sessions`; use the returned `session_token` for capabilities, resource reads, files, inspection, and deletion. |
| 409 `mode_mismatch` | `POST /v1/sessions` was called against a personal-mode server. | Use the configured personal bearer token for reads, or deliberately configure a multi-user server and restart it. |
| 401 `invalid_credentials` | The portal rejected the supplied student credentials. | Check the authorized account directly in the college portal and verify private dotenv quoting. Correct the values before retrying; repeated failed logins consume the login budget. |
| 401 `auth_expired` | The upstream portal or API student session expired. | Multi-user credentials are discarded after login: create a fresh API session. Personal mode retries one login using configured credentials; persistent failure needs a credential/portal check. |
| 409 `authentication_challenge` | The portal requires a CAPTCHA, OTP, or another interactive check. | Use the portal's normal human login flow. This API has no interactive challenge-solving flow; report the compatibility limit without sharing challenge tokens. |
| 429 `login_rate_limited` | This API's configured login-attempt window was exhausted, including failures. | Respect `Retry-After` (currently 60 seconds). Fix repeated bad credentials and reduce retries. Proxy/backend callers may share the socket-address budget. |
| 503 `session_capacity` | Active sessions plus pending logins reached `MAX_SESSIONS`. | Reuse valid sessions, delete sessions after use, and allow expired sessions to be cleaned up. Increase `MAX_SESSIONS` only after checking process memory and expected concurrency. |

The operator token and student password have different roles. Generating another API token does not change the college password or repair a portal login. See [authentication examples](api.md#authentication).

## Portal resources, filters, and files

| Error or symptom | Meaning | Action |
| --- | --- | --- |
| 404 `unsupported_resource` or capability `available=false` | No reviewed read route was discovered for this account/resource. | Check `/v1/capabilities` first. Confirm the module exists for your account in the portal; other accounts or institutions may expose a different subset. Use [compatibility](compatibility.md) before assuming coverage. |
| HTTP 200 with `data.empty=true` | The adapter recognized an explicit empty page or empty table. | Treat it as a successful empty result for those filters. Compare the selected portal view if unexpected. Do not replace it with an inferred total. |
| 502 `unsupported_layout` | The page, login form, or attachment response does not match a supported format. | Record the resource and safe error metadata; compare the current portal layout. Provide a synthetic/sanitized fixture using the [contributor guide](contributing.md#fixture-sanitation). A layout error is not an empty academic result. |
| 422 `invalid_filter` or `invalid_filters` | Names/options are undiscovered, duplicate, unsafe, malformed, or outside allowed values. | Fetch the resource without filters and use the exact `data.filters` names, input types, offered option values, and supported flags. URL-encode bracketed names; avoid duplicate parameters. |
| 422 `invalid_request` | Request schema or pagination validation failed. | Check required session-creation fields, field lengths, `page>=1`, and `1<=page_size<=500`. The error intentionally omits submitted input to protect credentials. |
| 404 `not_found` for a detail/file ID | The ID belongs to another session/resource, has been evicted, or came from a previous process. | Reuse IDs only in the session that returned them. Refresh the source resource after its cache expires, or create a fresh session and rediscover the link. Do not construct upstream URLs from opaque IDs. |
| Values appear unchanged immediately after a portal change | The successful parsed response is cached. | Check `meta.cache_hit` and `meta.fetched_at`. Wait for the configured TTL (60 seconds by default) and read again. `page`/`page_size` paginate that parsed response locally. |
| 403 `upstream_denied` | The portal refused access to that page or file. | Check the account's actual portal permissions. Changing the API bearer token does not grant additional college access. |
| 405 `operation_denied` | A discovered link/redirect would leave reviewed read/login operations. | Use returned safe resource/detail/file links. Report a legitimate layout change for review rather than disabling the read-only boundary. |
| 413 `response_too_large` | HTML/file content exceeds the configured bound. | Confirm the expected file/page and its size. Review `MAX_HTML_BYTES` or `MAX_DOWNLOAD_BYTES` and memory impact before raising a limit. |
| 502 `unsupported_encoding` | The portal returned compressed content despite the uncompressed-response requirement. | Report the response-format incompatibility with safe headers and the resource name. Do not bypass the transport's bounds. |

The fixed link registry retains at most 1,024 descriptors per adapter. A cached page can therefore still contain an evicted ID. See [cache and expiration behavior](configuration.md#caching-and-expiration) and the [filter/detail walkthrough](usage.md).

## DNS, destination checks, and timeouts

| Error | Meaning | Action |
| --- | --- | --- |
| 422 `invalid_institution` | The institution URL is not an HTTPS origin on port 443. | Use the college origin, such as `https://sahrdaya.etlab.in`, without a login path, query, fragment, IP address, or embedded credentials. |
| 422 `destination_denied` | A hostname/redirect is not allowlisted, or DNS returned a prohibited address. | Check the exact institution/attachment hostname and intended redirects. Explicit custom hosts must still resolve only to public addresses. Adding a host does not permit private or mixed public/private destinations. |
| 503 `upstream_unavailable` | DNS resolution, connection, or upstream service failed. | Confirm the authorized institution is reachable from the server and that outbound HTTPS/DNS works there. A browser on another machine may have a different network path. |
| 504 `upstream_timeout` | An upstream operation exceeded `UPSTREAM_TIMEOUT_SECONDS`. | Check portal availability and reduce concurrent/polling traffic. Review measured latency before adjusting the timeout; the deadline covers the operation and redirects. |
| 503 `upstream_rate_limited` | ETLab returned a rate-limit response. | Back off and reduce requests. Reuse sessions and caches. A new API token/session should not be used to evade portal limits. |
| 404 `upstream_not_found` | The portal could not find the requested page or file. | Refresh the resource's discovered links and confirm availability in the portal. The API cannot restore removed upstream content. |

The transport validates DNS before opening a socket, pins the connection to validated public addresses, verifies TLS certificates, and ignores proxy environment variables. There is no insecure TLS switch or private-address override. Use the [configuration reference](configuration.md) for hostname and timeout settings.

## Report a reproducible problem

Include the source version, Python/Compose version, server mode, resource or route name, HTTP status, safe error code, and request ID from `X-Request-ID` or `error.request_id`. Describe whether installation, `check`, health, documentation, or an authenticated resource read passed. Use synthetic values when showing request structure.

Do not include bearer tokens, passwords, cookies, raw HTML/HAR files, private URLs, academic values, or screenshots with student details. Follow [fixture sanitation](contributing.md#fixture-sanitation) for a layout reproducer and [security reporting](security.md#reporting-a-problem) for a vulnerability.
