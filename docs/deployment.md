# Deployment

Use one worker and one replica. The app's in-memory session store cannot be shared across processes, and process restarts invalidate multi-user sessions. The included setup deliberately publishes port 8000 only on loopback. Put a TLS reverse proxy in front of it for remote access.

For a hosted Dokploy service without a configured student username/password, follow the [Dokploy guide](dokploy.md): use the Dockerfile, `APP_MODE=multiuser`, and a new private operator `API_TOKEN`. The blank personal-mode `.env.example` template must be intentionally overridden for that setup.

## Local Python

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
cp .env.example .env
chmod 600 .env
# Edit .env: random API_TOKEN, mode, institution, and personal credentials if applicable.
uvicorn etlab_api.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers
```

Start from the directory containing `.env`. For a service manager, set its working directory to that directory and run it as an unprivileged dedicated user. Do not pass passwords or tokens in process arguments. Environment variables can also supply settings.

## Docker Compose

```sh
cp .env.example .env
chmod 600 .env
# Fill the required values before starting.
docker compose up --build -d
docker compose ps
curl --fail http://127.0.0.1:8000/healthz
```

The image runs as an unprivileged user. Compose uses a read-only filesystem, a temporary `/tmp`, no Linux capabilities, and no-new-privileges. `.env` is excluded from the image build and injected at runtime using `env_file`; it is not copied into the image. No database or volume is needed.

`/healthz` checks that the application process responds. It does **not** log in to ETLab, verify credentials, confirm a parser works, or prove any backing portal is healthy. Use an authenticated capability/resource request for that separate check.

To update:

```sh
docker compose up --build -d
docker compose ps
```

Treat updates as a session reset. Ask multi-user clients to create a fresh session after an update or restart. Keep the previous source/image version available for rollback; no data migration is needed because no student data is persisted.

## TLS reverse proxy

The following Caddy example assumes Caddy runs on the same host as the loopback-bound Python process or Compose service. Replace the domain with your own and point DNS at that host:

```caddyfile
api.example.edu {
    reverse_proxy 127.0.0.1:8000
}
```

Configure HTTPS using your reverse proxy's documented certificate setup. If the proxy is another container, use a private Docker network and the service name `etlab-api:8000` instead of that container's loopback; do not publish the application's port publicly.

Keep response caching disabled at the proxy. Do not log authorization headers, request bodies, response bodies, or query strings that may contain student filter values. Limit request sizes and rate-limit session creation at the proxy for internet-facing deployments. The supplied CLI, Docker command, and direct Uvicorn examples disable proxy-header rewriting (`--no-proxy-headers`), so the app's login limit uses the socket caller address. A proxy may therefore cause backend callers to share that app-level limit; apply end-user rate limits at the trusted proxy/backend instead of trusting arbitrary forwarded-IP headers.

The multi-user mode is intended for trusted backend applications. Add your own user authentication and enrollment policy in that backend; possession of the deployment token allows attempting a login with any permitted institution account. Personal-mode login attempts, including automatic reauthentication, also share a configured login-attempt budget.

## Token rotation

1. Generate a new random token using `python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
2. Replace `API_TOKEN` in the server secret/configuration and the trusted clients' secret stores.
3. Restart the single application process (`docker compose up -d --force-recreate` for Compose).
4. Verify a request with the new token succeeds and the old token is rejected.

There is no simultaneous old/new-token grace period. Restart also invalidates existing multi-user session tokens. Changing `.env` without restarting does not change the running settings.

## Operational checks

- Monitor process health separately from upstream resource availability. Login errors, parser errors, and timeouts are materially different failures.
- Use the response's request ID when investigating a failure. Logs should identify the error code without credentials or student payloads.
- Watch `SESSION_CAPACITY`, login throttling, upstream timeouts, and parser failures. Tune limits only after measuring actual usage.
- Keep the resource cache short and avoid scheduled bulk polling. The default interval applies per student session, not globally across all users.
- Back up source/configuration securely if needed. There is no academic archive to back up; protect personal-mode `.env` as a credential secret.

Check the [verification record](verification.md) for observed deployment evidence. Building an image and passing CI do not prove that a hosted service, TLS route, or authenticated portal integration works.
