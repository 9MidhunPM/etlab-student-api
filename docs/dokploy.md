# Deploying with Dokploy

Use a **Dokploy Application with the repository's Dockerfile**, one replica, and `APP_MODE=multiuser` for a shared service without a configured student username/password. Student credentials then arrive only in an authorized session-creation request and are discarded after login.

The current public source is [9MidhunPM/etlab-student-api](https://github.com/9MidhunPM/etlab-student-api), and the deployed API documentation is [https://etlab-api.midhunpm.in/docs](https://etlab-api.midhunpm.in/docs). On 2026-10-11, the initial Dokploy deployment completed with status `done`, a running healthy container, and passing HTTPS checks. Push-triggered deployment `OY4ycYOXOBuGnNFz03EPr` also completed for source commit `6ec610560cf9c9a5738ccd9ec28370487f323093`. The deployment uses all 19 example configuration keys, with a fresh private operator token, `APP_MODE=multiuser`, and empty student username/password values. See the [verification record](verification.md) for exact checks and remaining limits.

`.env.example` is an editable personal-mode template. Its blank credentials and blank token deliberately do not constitute a bootable configuration. A shared deployment must override the mode and set a newly generated operator token.

## Source and build

Create an Application in the intended Dokploy project/environment. Select the GitHub provider with access to your ETLab API repository (the current source is `9MidhunPM/etlab-student-api`), the intended branch (normally `main`), and build path `/`. Configure runtime variables in the Environment tab and keep one application replica in the Advanced/cluster settings. These are separate application settings in [Dokploy's Applications documentation](https://docs.dokploy.com/docs/core/applications).

Set the build configuration:

| Setting | Value |
| --- | --- |
| Build Type | `Dockerfile` |
| Dockerfile Path | `Dockerfile` |
| Docker Context Path | `.` |
| Docker Build Stage | Empty |
| Start command override | Empty; use the image's command |
| Replicas | `1` |
| Container/domain port | `8000` |

The path/context choices follow [Dokploy's Dockerfile build settings](https://docs.dokploy.com/docs/core/applications/build-type). The image already runs one Uvicorn worker as an unprivileged user and disables access logging and proxy-header rewriting. Do not switch to a generated Python/Nixpacks command or add workers/replicas: sessions and cache entries live in one process.

No database, archive volume, student `.env` mount, or host port publication is required. Dockerfile Applications do not automatically inherit the hardening settings from the repository's local Compose file; apply platform runtime restrictions separately if needed.

## Runtime environment

Generate a **new deployment-only operator token**:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Store that value in Dokploy's runtime Environment configuration and the trusted backend's secret store. Do not reuse the local development token or include any real token in Git, documentation, image build arguments, or logs. The application's Docker build requires no API secret; leave Build Time Arguments and Build-time Secrets empty.

Use the `.env.example` defaults with these intentional overrides:

```dotenv
APP_MODE=multiuser
API_TOKEN=<replace-with-the-new-random-operator-token>
ETLAB_BASE_URL=https://sahrdaya.etlab.in
ETLAB_USERNAME=
ETLAB_PASSWORD=
ETLAB_ALLOWED_HOSTS=*.etlab.in
ETLAB_ATTACHMENT_HOSTS=
CACHE_TTL_SECONDS=60
CACHE_MAX_BYTES=8388608
SESSION_IDLE_SECONDS=1800
SESSION_MAX_SECONDS=28800
UPSTREAM_TIMEOUT_SECONDS=20
UPSTREAM_INTERVAL_SECONDS=1
MAX_DOWNLOAD_BYTES=26214400
MAX_HTML_BYTES=5242880
MAX_SESSIONS=100
LOGIN_ATTEMPTS_PER_MINUTE=5
CORS_ORIGINS=
DOCS_ENABLED=true
```

Replace the token placeholder before deploying; a blank/short token prevents startup. Keep the two ETLab credential keys empty or omit them entirely. In multi-user mode, `ETLAB_BASE_URL` is not a shared student's account: each `POST /v1/sessions` request supplies its own allowed institution origin and credentials.

Use exact `ETLAB_ATTACHMENT_HOSTS` entries only when a permitted institution actually serves needed files on another host. Leave CORS empty for backend-to-backend access; add exact browser origins only when the frontend authentication design requires them. See [all configuration rules](configuration.md).

## Domain and HTTPS

The current deployment uses `etlab-api.midhunpm.in`. For another installation, confirm its API hostname, point its DNS at the intended Dokploy server, and add it in Domains with:

| Domain setting | Value |
| --- | --- |
| Host | Your confirmed API hostname |
| Path | `/` |
| Internal Path | `/` |
| Container Port | `8000` |
| HTTPS | Enabled |
| Certificate | Let's Encrypt, or the configured valid certificate |

These fields and TLS options are described in [Dokploy's Domains guide](https://docs.dokploy.com/docs/core/domains). Route through Dokploy's Traefik service; leave direct host port publication unset. Public docs/schema and `/healthz` are intentional; data routes remain bearer-protected.

The app sees the proxy's socket address because forwarded-IP rewriting is disabled. Backend callers may share its login-attempt budget. Apply end-user admission/rate limits in the trusted backend/proxy and retain the app's own bounded login and session controls.

## Deploy and verify

Save the source, runtime environment, and domain settings, then deploy. Wait for the build/deployment to complete and the container to become healthy before checking the HTTPS endpoint. A build marked successful is separate evidence from application health and working domain routing.

Check the confirmed HTTPS base URL:

```sh
# Replace this hostname if deploying a separate installation.
curl --fail-with-body https://etlab-api.midhunpm.in/healthz
curl --fail-with-body https://etlab-api.midhunpm.in/openapi.json
```

Check that `/docs` renders in a browser, and an unauthenticated `/v1/me/profile` request returns 401 with `Cache-Control: no-store`. These checks require no student password. In multi-user mode, the operator token cannot read student data; its intended operation is admitting `POST /v1/sessions`. Use the [multi-user examples](api.md#multi-user-mode) when authorized student login testing is requested.

Do not put a username/password into the deployment merely to make its health check pass. `/healthz` checks the worker and never logs in to ETLab. A running credential-free multi-user server is valid; institution compatibility is verified separately for each authorized session.

## Updates and token rotation

Deploy the intended Git commit and retain its deployment record for rollback. Every replacement/restart clears multi-user sessions, so clients must handle a 401 by obtaining a fresh session through their authenticated backend flow.

For operator-token rotation, generate a new value, update runtime configuration and trusted clients, then redeploy. There is no old/new token grace period. Never retrieve or publish existing runtime secrets just to verify that the service works; verify behavior with the appropriate authorized token privately.
