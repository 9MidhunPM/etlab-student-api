# Deployment

Use **one worker and one replica**. Sessions, portal cookies, caches, and link IDs live in that process; replacing it invalidates multi-user sessions. The supplied Python command and Compose file bind the API to loopback. Remote access needs a TLS reverse proxy.

For a first installation, start with [getting started](getting-started.md). For a shared Dokploy service, use the [Dokploy guide](dokploy.md) with `APP_MODE=multiuser` and a private operator token. Student credentials are unnecessary for a multi-user server to start.

Shell examples below use Bash on Linux/macOS. On Windows, use a Bash environment such as WSL and protect the configuration with the appropriate filesystem permissions.

## Local Python

Install the package and prepare `.env` using the [Python setup guide](getting-started.md). From the directory containing that file, with the virtual environment activated:

```sh
etlab-api check
etlab-api serve
```

`serve` runs one worker on `127.0.0.1:8000`, with access logging and proxy-header rewriting disabled. Choose another local port with `etlab-api serve --port 8001`. Stop the foreground process with Ctrl+C. Run the same commands again to start it.

For a service manager, set the working directory to the directory containing `.env`, use the installed virtual environment's absolute `etlab-api` path, and run as an unprivileged dedicated user. Settings are loaded once at startup. Stop and start the process after changing its environment or `.env`; do not pass passwords or tokens in command arguments.

## Docker Compose

### Prerequisites

Install Docker Engine with the **Compose v2 plugin**, or Docker Desktop with Linux containers. The Docker daemon must be running, and your account must be permitted to use it. Host Python is unnecessary: the image includes Python and the package CLI. Git is needed to clone the repository; a browser or curl can check the running service.

Confirm the tools are available:

```sh
docker version
docker compose version
docker compose up --help
```

The help output must include `--wait` and `--wait-timeout`. Upgrade Compose if it does not. These examples use `docker compose`, rather than the older `docker-compose` command. Port 8000 must be available on the host; see [port conflicts](troubleshooting.md#startup-and-local-tools) if it is occupied.

### Configure and start

Run these commands from the repository root. Preserve an existing configuration when repeating setup:

```sh
test -e .env || cp .env.example .env
chmod 600 .env
docker compose build
docker compose run --rm --no-deps etlab-api etlab-api token
```

The final command prints a newly generated token. Put it in `API_TOKEN` using your editor or secret manager. Do not paste the token into a shell command or publish the output. It does not validate configuration or log in to ETLab.

Choose the server mode in `.env`:

| Mode | Required values |
| --- | --- |
| Personal student server | `APP_MODE=personal`, a new `API_TOKEN`, your college's `ETLAB_BASE_URL`, `ETLAB_USERNAME`, and `ETLAB_PASSWORD`. |
| Shared backend service | `APP_MODE=multiuser` and a new private operator `API_TOKEN`; leave student username/password empty. Clients provide their own institution and credentials when creating a session. |

The remaining defaults are described in [configuration](configuration.md). Check the edited values without contacting ETLab, then start and wait for container health:

```sh
docker compose run --rm --no-deps etlab-api etlab-api check
docker compose up --build --wait --wait-timeout 120
docker compose ps
curl --fail-with-body http://127.0.0.1:8000/healthz
```

A successful check prints `Configuration valid. No upstream connection attempted.` A successful health request returns `{"status":"ok"}`. Open [Swagger UI](http://127.0.0.1:8000/docs) next and follow [first requests](usage.md) with the appropriate bearer token.

Compose's [`--wait` option](https://docs.docker.com/reference/cli/docker/compose/up/) waits for services to be running or healthy and implies detached mode. A timeout can leave the container running or restarting: inspect `ps` and logs before trying again. `/healthz` proves only that the worker responds; it does not verify DNS, student credentials, portal health, or parser compatibility.

The image runs as an unprivileged user. Compose supplies a read-only filesystem, temporary `/tmp`, no Linux capabilities, and no-new-privileges. `.env` is excluded from the image build and supplied through `env_file` at container creation. No database or persistent volume is required. The container listens on port 8000 internally; Compose publishes it only at `127.0.0.1:8000` on the host.

### Inspect and stop

```sh
docker compose ps
docker compose logs --tail=100 etlab-api
docker compose logs --follow --tail=100 etlab-api
```

Ctrl+C stops following logs and leaves the service running. Startup logs can help diagnose invalid settings or a process crash. Do not publish logs without checking them for private information, and do not use `docker compose config` or container environment dumps to share diagnostics: resolved configuration can contain credentials.

To stop and remove the service container and its Compose network:

```sh
docker compose down
```

This leaves source files and `.env` on the host. In-memory sessions are cleared. Start again with `docker compose up --wait --wait-timeout 120`.

### Reload configuration and update

After editing `.env`, validate and **recreate** the container:

```sh
docker compose run --rm --no-deps etlab-api etlab-api check
docker compose up --force-recreate --wait --wait-timeout 120
```

[`docker compose restart`](https://docs.docker.com/reference/cli/docker/compose/restart/) reuses the container's existing environment; it does not load changed `.env` values. All replacements and restarts clear student sessions. Multi-user clients must create new sessions through their authenticated backend flow.

For a source update, record the current commit and retain its image before updating your checkout. For example, while the current service exists:

```sh
git rev-parse HEAD
docker image tag "$(docker compose images -q etlab-api)" etlab-api:previous
```

Use an unused local image tag for each release you need to retain; replace `etlab-api:previous` in these examples if that tag already protects another release. From the intended new source version:

```sh
docker compose up --build --wait --wait-timeout 120
docker compose ps
curl --fail-with-body http://127.0.0.1:8000/healthz
```

Repeat the authentication-boundary checks and any separately authorized student checks after deployment. Keep a previously verified source checkout or a separately tagged image until the new version passes those checks.

For rollback, use the retained checkout with the current protected configuration, or create a local `compose.rollback.yaml` selecting the retained image:

```yaml
services:
  etlab-api:
    image: etlab-api:previous
    pull_policy: never
```

From the same project directory, start it using both Compose files:

```sh
docker compose -f compose.yaml -f compose.rollback.yaml up --no-build --force-recreate --wait --wait-timeout 120
curl --fail-with-body http://127.0.0.1:8000/healthz
```

Keep the same Compose project name and service port so the retained release replaces the intended service. Keep the current working tree and retained image available. A rollback also resets sessions; no academic data migration is needed because the app persists no student data. When returning to the current source version, omit the rollback override and use the normal update command.

## TLS reverse proxy

The following Caddy example assumes Caddy runs on the same host as the loopback-bound Python process or Compose service. Replace the domain with your own and point DNS at that host:

```caddyfile
api.example.edu {
    reverse_proxy 127.0.0.1:8000
}
```

Configure HTTPS using your reverse proxy's documented certificate setup. If the proxy is another container, use a private Docker network and the service name `etlab-api:8000` instead of that container's loopback; do not publish the application's port publicly.

Keep response caching disabled at the proxy. Do not log authorization headers, request bodies, response bodies, or query strings that may contain student filter values. Limit request sizes and rate-limit session creation at the proxy for internet-facing deployments. The supplied commands disable proxy-header rewriting, so the app's login limit uses the socket caller address. A proxy may cause backend callers to share that app-level limit; apply end-user rate limits at the trusted proxy/backend.

Multi-user mode is intended for trusted backend applications. Add user authentication and enrollment policy there; possession of the operator token allows attempting a login with any permitted institution account.

## Token rotation

1. Generate a fresh token with `etlab-api token`, or `docker compose run --rm --no-deps etlab-api etlab-api token`.
2. Update `API_TOKEN` in the server's protected configuration and trusted clients' secret stores.
3. Restart the Python process, or recreate the Compose container with `docker compose up --force-recreate --wait --wait-timeout 120`.
4. Confirm `/healthz` responds and the old operator token is rejected. In personal mode, verify a read with the new token; in multi-user mode, verify the new token admits an authorized session creation and use its student token for reads.

There is no simultaneous old/new-token grace period. Rotation resets existing sessions. Editing `.env` alone does not change running settings.

## Operational checks

- Monitor worker liveness separately from upstream availability. Login errors, parser failures, and timeouts need different responses.
- Record the HTTP status, safe error code, and `X-Request-ID` when investigating a failure. The API does not provide a metrics endpoint or structured per-request error log.
- Watch the `session_capacity` error and `MAX_SESSIONS` setting, login throttling, upstream timeouts, and parser failures. Tune limits after measuring actual usage.
- Keep the resource cache short and avoid bulk scheduled polling. The upstream interval applies per student session, rather than globally across all users.
- Protect personal-mode `.env` as a credential secret. There is no academic archive to back up.

See [troubleshooting](troubleshooting.md), the [documentation index](README.md), and the dated [verification record](verification.md). An image build or passing CI does not establish hosted TLS routing or authenticated portal compatibility.
