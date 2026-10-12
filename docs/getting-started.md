# Getting started

This guide takes you from a fresh checkout to a running local API. Choose personal mode for one account or multi-user mode for a trusted backend integration. Both expose the same read-only academic endpoints.

## Prerequisites

- Linux or macOS with Bash, Git, and Python **3.12 or later**. CI checks Python 3.12 and 3.13. Confirm `python3 --version`; choose a suitable interpreter if the system default is older. Install Python from [python.org](https://www.python.org/downloads/) or your operating system's package manager.
- Outbound HTTPS and public DNS access to your permitted ETLab institution, plus internet access for initial dependency installation.
- For personal mode or a student session: an authorized student account and the institution's HTTPS origin, for example `https://sahrdaya.etlab.in`.
- Optional curl for terminal requests. Examples using `--fail-with-body` require curl 7.76 or later; Swagger and the Python client are alternatives.
- For containers, use [Docker Compose setup](deployment.md#docker-compose) instead. Host Python is unnecessary for the server; the runnable Python client still requires it.

Shell commands below use Bash. On macOS, which commonly starts zsh, run `bash` before copying Bash-specific prompts from the usage guide. No database or academic-history volume is required.

## Get the source

```bash
git clone https://github.com/9MidhunPM/etlab-student-api.git
cd etlab-student-api
```

If you already have a checkout, open its directory and continue. Keep its existing `.env`.

## Install with Python and pip

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
etlab-api --help
```

Expected help lists `serve`, `token`, and `check`. This installs the application and runtime dependencies. Contributor tools are optional:

```bash
python -m pip install -e '.[dev]'
```

An editable development install reflects changes under `src` immediately. For a normal `pip install .`, reinstall after changing source. Reactivate with `. .venv/bin/activate` whenever opening another terminal.

## Install with uv instead

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first, then choose this path instead of pip:

```bash
uv venv --python 3.12
. .venv/bin/activate
uv pip install .
etlab-api --help
```

uv can obtain the requested Python version if it is absent. For contributors use `uv pip install -e '.[dev]'`. These commands use [uv's environment interface](https://docs.astral.sh/uv/pip/environments/). The project declares dependency ranges and currently has no committed lockfile.

## Create private configuration

From the checkout root:

```bash
test -e .env || cp .env.example .env
chmod 600 .env
etlab-api token
```

The copy creates `.env` only if missing. Keep existing values when repeating setup. `token` prints a newly generated secret; paste it into `API_TOKEN` using your editor and keep it private. `.env` is excluded from Git and the container build.

Choose a mode before validating:

| Setting | Personal | Multi-user |
| --- | --- | --- |
| `APP_MODE` | `personal` | `multiuser` |
| `API_TOKEN` | New token granting access to your configured account | New operator token held by the server and trusted backend |
| `ETLAB_BASE_URL` | Your allowed institution's HTTPS origin | Keep a valid allowed origin; each student login supplies its institution |
| `ETLAB_USERNAME`, `ETLAB_PASSWORD` | Your student credentials | Empty or omitted |

### Personal mode

Edit these keys in `.env`; replace every placeholder:

```dotenv
APP_MODE=personal
API_TOKEN=<replace-with-the-generated-token>
ETLAB_BASE_URL=https://sahrdaya.etlab.in
ETLAB_USERNAME=<your-student-username>
ETLAB_PASSWORD='<your-student-password>'
```

The token must be at least 32 characters; use the generated random value. Quote passwords containing spaces or `#`. For passwords containing quote characters, follow dotenv quoting rules rather than copying the placeholder literally. Enter secrets in your private configuration editor instead of command arguments or issue reports.

Personal credentials remain in server configuration so the adapter can attempt one fresh login after upstream expiry. Your API token is separate from the college password. The first capability/resource/download operation triggers login; inspecting or clearing the API session does not contact ETLab.

### Multi-user mode

For a server without stored student credentials, edit `.env`:

```dotenv
APP_MODE=multiuser
API_TOKEN=<replace-with-the-generated-operator-token>
ETLAB_BASE_URL=https://sahrdaya.etlab.in
ETLAB_USERNAME=
ETLAB_PASSWORD=
```

It can start without a student password. A trusted backend uses the operator token for `POST /v1/sessions`, supplying the student's institution and credentials. The response returns a separate student token for reads and deletion. The operator token cannot read student data; keep it out of frontend bundles and model prompts.

Student credentials are discarded after the login attempt. Expired upstream authentication requires a new session. Follow the [usage walkthrough](usage.md) after startup.

### Other institutions

Use an HTTPS origin with no path, query, fragment, or embedded credentials. ETLab subdomains are permitted by default. For a custom hostname, deliberately extend the allowlist:

```dotenv
ETLAB_ALLOWED_HOSTS=*.etlab.in,portal.example.edu
```

Allowlisting does not establish compatibility or permit private/IP destinations. Check [configuration rules](configuration.md) and [institution evidence](compatibility.md). Interactive login challenges and changed layouts may require adapter work.

## Validate and start

```bash
etlab-api check
etlab-api serve
```

A successful check prints:

```text
Configuration valid. No upstream connection attempted.
```

Invalid settings exit with status 2 and identify configuration keys without printing values. This check validates settings, origin syntax, and the allowlist. It does not resolve DNS, connect upstream, verify credentials, or prove a parser works.

`serve` listens on `127.0.0.1:8000` with one worker, no access log, and no proxy-header rewriting. Keep that terminal running; stop it with **Ctrl+C**. For another local port:

```bash
etlab-api serve --port 8001
```

Use the same port in clients. Keep one worker and one application replica. Restarts invalidate in-memory multi-user tokens. Settings load from `.env` in the current working directory; process environment variables take precedence. Editing `.env` requires restarting the process.

For remote access, follow [TLS deployment](deployment.md#tls-reverse-proxy). Binding `--host 0.0.0.0` exposes network interfaces and does not provide HTTPS by itself.

## Verify the running process

In a second Bash terminal:

```bash
curl --fail http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"status":"ok"}
```

Open [Swagger UI](http://127.0.0.1:8000/docs), [ReDoc](http://127.0.0.1:8000/redoc), or `/openapi.json`. They are public with the default `DOCS_ENABLED=true`. Health and docs work without contacting ETLab.

Next, use the [usage walkthrough](usage.md) for authorization, capability discovery, a supported resource, and cleanup. If a checkpoint fails, use [troubleshooting](troubleshooting.md). Process liveness and upstream account reads are separate checks.

## CLI reference

| Command | Behavior |
| --- | --- |
| `etlab-api --help` | Lists commands without loading credentials. |
| `etlab-api token` | Prints a random token without loading settings or contacting ETLab. |
| `etlab-api check` | Validates local settings and origin policy, then exits. |
| `etlab-api serve` | Validates configuration, then starts one local worker. |
| `etlab-api serve --host 127.0.0.1 --port 8001` | Uses the requested host/port; defaults are loopback and 8000. |

See the [documentation checkpoints](README.md#checkpoints) for what each check establishes.
