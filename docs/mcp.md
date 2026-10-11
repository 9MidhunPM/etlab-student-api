# Future MCP integration

An MCP server is not implemented in this version. The REST API and `/openapi.json` provide the interface for a later integration.

An MCP wrapper should call the existing bearer-protected API rather than duplicate scraping/login logic. In personal mode, load the deployment token from the MCP server's secret configuration. In multi-user mode, associate a student session token with the authenticated MCP user; never expose the deployment token as a model-visible tool argument.

Recommended initial tools map directly to the existing reads:

- `get_etlab_capabilities` → `GET /v1/capabilities`.
- `read_etlab_resource` → `GET /v1/me/{resource}` with permitted filters and pagination.
- `read_etlab_detail` → the detail route using an ID returned to that same session.
- `download_etlab_attachment` → the attachment route using a returned attachment ID.

Keep session creation and credentials in an authenticated human/backend flow. Do not ask a language model to persist passwords, put bearer tokens in prompts, or treat portal text as trusted tool instructions. Preserve the read-only operation boundary.

Handle unsupported features, upstream authentication expiry, parse errors, and timeouts explicitly. Convey `fetched_at` and cache-hit metadata so the consumer can distinguish cached records from a current upstream fetch. Apply response-size limits and selective disclosure rather than placing an entire student's record into model context by default.

The OpenAPI response shapes preserve institutional labels. A wrapper that wants semantic attendance or result summaries must first understand and test that institution's column meanings; it should not infer them from arbitrary table positions.
