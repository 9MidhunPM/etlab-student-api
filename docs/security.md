# Privacy and security model

The server retrieves private academic information using credentials supplied by the operator or student. Run it only for accounts you are authorized to access. The API is read-only for academic operations; it does not submit assignments, leave applications, payments, profile edits, placement applications, or survey responses.

## Authentication boundaries

Personal mode has one deployment bearer token granting the configured student's reads. Multi-user mode uses the deployment token only to admit login requests, then returns independent random bearer tokens for student data. A deployment token cannot read an existing multi-user student session. Token lookup uses a hash, and each session has its own cookie jar, discovered links, cache, and synchronization lock. An adapter retains at most 1,024 opaque detail/file descriptors and hashes the username identity used to distinguish same-account reauthentication.

Token possession is authorization. Serve remote traffic over TLS. Store tokens in a secret manager/backend session store and send them in the Authorization header. No browser frontend authentication, account registration, tenant administration, or refresh-token system is built into this project.

## Data retention

- Cookies, discovered detail/attachment IDs, and successful parsed page data are held only in process memory.
- Parsed response caches default to 60 seconds and are bounded per session. There is no permanent academic database or background scrape.
- Multi-user passwords are discarded after login. Personal-mode passwords remain in configuration to support a fresh upstream login.
- Attachment bodies are bounded in memory, returned to the requesting client, and not written to files or retained in the cache.
- Multi-user expiry, session deletion, and shutdown clear session state. A process restart invalidates those tokens.
- Personal same-account automatic reauthentication retains current link descriptors for its read retry; successful login under another username clears them. Descriptor eviction returns a missing-link error.
- API responses use `Cache-Control: no-store`. Your client must also avoid unnecessary persistence of sensitive data.

Operating-system memory, core dumps, swap, reverse-proxy logs, and client storage are deployment concerns. Avoid request/response body logging and restrict access to the service account and `.env`. These are normal operational boundaries, not encrypted persistence guarantees.

## Upstream destinations

Only HTTPS DNS hostnames on port 443 are accepted. Default institution hosts are subdomains of `etlab.in`; operator configuration can add other hosts. Each connection resolves and validates addresses, then connects to a validated public IP while retaining the hostname for TLS certificate verification. DNS uses cancellable asynchronous resolution with a bounded lookup lifetime, so DNS work does not leave a blocking resolver thread alive during shutdown. Private/reserved addresses, mixed DNS answers, unexpected origins, unsafe redirects, and IP-literal URLs are rejected.

Page requests and login credentials stay on the current institution origin. Attachment requests use the current institution or explicit attachment-host allowlist. External attachment links must be discovered on an academic page and have a recognized file extension and safe URL shape. An external attachment origin uses a separate client and receives no institutional cookies. Clients request opaque attachment IDs returned by a parsed page; they cannot provide arbitrary download URLs.

Response sizes, total upstream operation time, redirect count, connection concurrency, and upstream request interval are bounded. Login throttling and session capacity limit uncontrolled session creation. DNS validation is not a general guarantee that an approved institution's content is harmless; clients should still handle downloaded files as external content.

## Read-only enforcement

Authentication requires submitting credentials. The adapter permits identified read-only GET filter forms with select, text/search, date, local datetime, and number controls; values are validated before the filtered request. It rejects action links/forms such as submission, payment, editing, deletion, or approval, and hides credential/action filter controls. When a form cannot be identified as read-only, it is exposed as unsupported for filtering instead of submitted.

No request ever needs a CAPTCHA/MFA bypass. If the portal requires an interactive challenge, the API reports that requirement rather than automating around it. Do not add write actions while treating them as reads.

## Reporting a problem

Report the affected version, mode, error code, request ID, and a synthetic reproduction. Share a privately redacted HTML fixture only after checking it for credentials, cookies, personal names, email addresses, student identifiers, grades, addresses, and academic documents. Never include `.env`, an Authorization header, a raw HAR, an upstream session cookie, or a live student payload in a public issue.
