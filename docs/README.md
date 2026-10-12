# Documentation

Start with your goal, then use the reference pages for a particular setting or endpoint. Shell examples use Bash on Linux/macOS; API payloads are platform-independent. Student records and tokens in examples are synthetic or placeholders.

## Personal users

1. [Getting started](getting-started.md): prerequisites, installation, private configuration, validation, and startup.
2. [Usage walkthrough](usage.md): authorize in Swagger or a client, discover modules, read data, follow filters/details/files, and clean up.
3. [Troubleshooting](troubleshooting.md): identify failures using symptoms, error codes, and request IDs.
4. [Compatibility](compatibility.md): understand what has actually been verified for your institution.

## Integration developers and operators

| Guide | What it answers |
| --- | --- |
| [Multi-user setup](getting-started.md#multi-user-mode) | How do I start a server without stored student credentials? |
| [Usage walkthrough](usage.md) and [runnable client](../examples/README.md) | How do I create a session, use the student token, and delete it? |
| [API reference](api.md) | What are the endpoints, response models, filters, and errors? |
| [Configuration](configuration.md) | What are the defaults, validation rules, cache limits, and expiry behavior? |
| [Deployment](deployment.md) | How do I run Python/Compose, add TLS, update, rotate tokens, and roll back? |
| [Dokploy](dokploy.md) | Which build, environment, replica, and domain settings does the hosted deployment need? |
| [Security](security.md) | What do tokens authorize and how are credentials, destinations, and read-only access handled? |
| [Verification record](verification.md) | Which checks were observed, when, and what remains unverified? |
| [Contributing](contributing.md) | How is the code organized, tested, and extended for another layout? |
| [Future MCP integration](mcp.md) | How could a later MCP wrapper use this REST API? No MCP server is implemented. |

## Checkpoints

| Check | What success establishes |
| --- | --- |
| `etlab-api check` | Local configuration is acceptable; no DNS or ETLab request is made. |
| `GET /healthz` | The application process responds. |
| `/docs`, `/redoc`, `/openapi.json` | Enabled documentation/schema routes are served. |
| `GET /v1/capabilities` | Authentication and account-specific module discovery work for that session. |
| An available resource read | That particular base page was fetched and parsed. |
| A filter, detail, or attachment request | That particular linked workflow works; check each separately. |

Fixture tests and deployment health cannot establish the correctness of a student's portal values. Preserve this distinction when reporting issues or adding compatibility evidence.
