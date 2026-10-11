"""Local setup helpers; live credentials and upstream response bodies are never printed."""

import argparse
import secrets
import sys
from urllib.parse import urlsplit

from pydantic import ValidationError

from etlab_api.errors import EtlabError
from etlab_api.settings import Settings
from etlab_api.upstream import HostPolicy


def main() -> None:
    parser = argparse.ArgumentParser(description="Run or configure the ETLab student API")
    subcommands = parser.add_subparsers(dest="command")
    serve = subcommands.add_parser("serve", help="Run one API worker")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    subcommands.add_parser("token", help="Generate a new private API token")
    subcommands.add_parser("check", help="Validate configuration without contacting ETLab")
    args = parser.parse_args()
    if args.command == "token":
        print(secrets.token_urlsafe(32))
        return
    if args.command is None:
        parser.print_help()
        return
    try:
        settings = Settings()
        validated = HostPolicy(settings.etlab_allowed_hosts).validate(settings.etlab_base_url)
        parsed = urlsplit(validated)
        if parsed.path != "/" or parsed.query:
            raise EtlabError(
                "invalid_institution", "ETLAB_BASE_URL must contain only the HTTPS origin", 422
            )
    except ValidationError as exc:
        keys = ", ".join(
            ".".join(map(str, item["loc"])) or "configuration"
            for item in exc.errors(include_input=False, include_url=False)
        )
        print(f"Invalid configuration ({keys}). Check .env.example.", file=sys.stderr)
        raise SystemExit(2) from None
    except EtlabError as exc:
        print(f"Invalid configuration: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from None
    if args.command == "check":
        print("Configuration valid. No upstream connection attempted.")
        return
    import uvicorn

    uvicorn.run(
        "etlab_api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=1,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
