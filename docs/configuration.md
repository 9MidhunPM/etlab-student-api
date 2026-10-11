# Configuration

Settings are loaded from `.env` in the current working directory. Process environment variables take precedence. Copy `.env.example`, generate a random token, and keep `.env` out of version control. Unknown keys are ignored; supported keys are below.

The example selects personal mode while leaving credentials/token blank, so it must be filled before local personal-mode startup. For a shared hosted service with no stored student credentials, deliberately set `APP_MODE=multiuser`, leave `ETLAB_USERNAME`/`ETLAB_PASSWORD` empty, and generate a separate private operator token. See the [Dokploy runtime configuration](dokploy.md#runtime-environment) for the complete credential-free deployment template.

| Variable | Default | Meaning |
| --- | --- | --- |
| `APP_MODE` | `personal` | `personal` for one configured student; `multiuser` for isolated student sessions. |
| `API_TOKEN` | Required; no default | Deployment bearer token, at least 32 characters. Generate it using `secrets.token_urlsafe(32)`. |
| `ETLAB_BASE_URL` | `https://sahrdaya.etlab.in` | Personal-mode college origin. HTTPS on port 443 only; no path, query, fragment, or embedded credentials. |
| `ETLAB_USERNAME` | Required in personal mode | Student login username. Multi-user requests provide their own credentials. |
| `ETLAB_PASSWORD` | Required in personal mode | Student login password. Quote values as needed for dotenv syntax, especially `#`, spaces, or newlines. |
| `ETLAB_ALLOWED_HOSTS` | `*.etlab.in` | Comma-separated allowed institution hostnames. Replaces the default list when set. |
| `ETLAB_ATTACHMENT_HOSTS` | Empty | Comma-separated extra file hosts; the current institution host is always included. No institutional cookies are sent to an external host. |
| `CACHE_TTL_SECONDS` | `60` | Parsed-page and capability cache lifetime. Set `0` to disable caching. |
| `CACHE_MAX_BYTES` | `8388608` | Per-session serialized cache budget: 8 MiB. Larger responses are returned without caching. |
| `SESSION_IDLE_SECONDS` | `1800` | Multi-user idle timeout in seconds. |
| `SESSION_MAX_SECONDS` | `28800` | Multi-user maximum lifetime in seconds. |
| `UPSTREAM_TIMEOUT_SECONDS` | `20` | Maximum time for an upstream operation, including its redirects. |
| `UPSTREAM_INTERVAL_SECONDS` | `1` | Minimum gap between upstream operations for a session. `0` disables the gap. |
| `MAX_DOWNLOAD_BYTES` | `26214400` | Maximum decoded attachment size: 25 MiB. |
| `MAX_HTML_BYTES` | `5242880` | Maximum decoded page-response size: 5 MiB. |
| `MAX_SESSIONS` | `100` | Maximum active and pending multi-user sessions combined. |
| `LOGIN_ATTEMPTS_PER_MINUTE` | `5` | Login attempts per 60-second window, including failures: per caller address in multi-user mode, one shared bucket in personal mode. |
| `CORS_ORIGINS` | Empty | Comma-separated browser origins permitted to call the API. Normally leave empty for backend clients. |
| `DOCS_ENABLED` | `true` | Expose `/docs`, `/redoc`, and `/openapi.json`. Set `false` to disable public schema routes. |

Use exact custom hostnames whenever possible:

```dotenv
ETLAB_ALLOWED_HOSTS=*.etlab.in,portal.example.edu
ETLAB_ATTACHMENT_HOSTS=files.example.edu
CORS_ORIGINS=https://app.example.edu
```

An allowlisted hostname must still resolve exclusively to public addresses. Private, loopback, link-local, multicast, reserved, mixed public/private DNS results, literal IP destinations, non-HTTPS URLs, and unexpected redirects are rejected. Allowlisting does not override those checks. There is no insecure TLS switch.

Timeout, session duration, session count, and body-size values must be positive. The idle timeout must not exceed the maximum session lifetime. Cache TTL and upstream interval may be zero but not negative. Invalid configuration prevents application startup. Secret values are hidden from configuration diagnostics.

## Caching and expiration

Each session has its own cache. Cache keys include resource, upstream filters, and detail ID. API pagination uses the cached parsed page rather than fetching once per page. Successful parsed results are cached; errors and attachment bodies are not. Each session retains at most 128 cache entries within `CACHE_MAX_BYTES` of serialized content. Results above that budget are returned without caching, and object overhead is additional to the serialized budget. The `fetched_at` timestamp identifies the upstream fetch, so it stays the same on a cache hit. Expired entries are removed during reads and periodic cleanup.

The adapter's opaque detail/file registry is separately bounded to 1,024 descriptors across resources, with least-recently-used eviction. This cap is fixed in the adapter rather than a `.env` setting. After an ID is evicted, refresh the source page after its cache entry expires or start a fresh API session; cached page content can still include an older ID.

The cache is on demand: there is no scheduled scrape, archive, database, or persisted academic history. `Cache-Control: no-store` tells HTTP clients and proxies not to store responses. Setting `CACHE_TTL_SECONDS=0` forces each academic request to fetch upstream and increases portal traffic.

Multi-user session expiry is checked during authentication and periodic cleanup. Any authenticated request extends the idle expiry, but never the maximum lifetime. Session inspection therefore counts as activity. The `expires_at` value from session creation is the maximum expiry; session inspection returns the earlier current idle/maximum expiry. Personal sessions do not use these multi-user lifetime limits.

Run a single worker and replica because all sessions and cache entries live in that process. A restart clears upstream cookies and all multi-user tokens; personal mode can log in again from `.env`.

## Tokens and credentials

Keep `API_TOKEN` in the server and trusted application backend. In multi-user mode, it grants session creation, not access to existing students' data. Student session tokens are bearer secrets and authorize their own data. Delete a session after use, avoid browser local storage when possible, and never put a token in a URL query parameter.

Personal credentials stay in server configuration for automatic reauthentication. Successful reauthentication with the same username/institution retains the adapter's current detail/file descriptors so the pending read can retry; changing the username clears descriptors. The adapter stores the account identity as a hash and retains no password. Multi-user credentials are used only for the initial login and are not stored in the managed session. A new login is required after upstream authentication expires in multi-user mode.

See [token rotation](deployment.md#token-rotation) and the [security model](security.md).
