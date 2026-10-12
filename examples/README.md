# Runnable student client

After [installing the project](../docs/getting-started.md), activate its Python environment and run these commands from the checkout root. Start the API in another terminal first.

```sh
python examples/read_student.py
python examples/read_student.py --resource attendance
python examples/read_student.py --resource attendance --show-data
```

The default personal mode lists available resources. A resource read prints counts, the empty flag, cache status, and fetch time. `--show-data` explicitly prints the academic response, which can contain private student information. Personal mode uses the deployment token and preserves the shared server session.

For a multi-user deployment:

```sh
python examples/read_student.py --mode multiuser \
  --institution https://sahrdaya.etlab.in
python examples/read_student.py --mode multiuser \
  --institution https://sahrdaya.etlab.in --resource attendance
```

Add `--api-url https://your-api.example` to use your own HTTPS server. Local HTTP accepts `localhost` or literal loopback addresses only. API URLs support custom ports; institution URLs require HTTPS port 443. API and institution URLs must be root URLs without credentials, paths, queries, or fragments.

The client uses `ETLAB_API_TOKEN` from the process environment when present; otherwise it requests the deployment token through a hidden terminal prompt. It does not read `.env`. In multi-user mode it also requests the student username and password through hidden prompts. Do not pass credentials as arguments. If hidden input is unavailable, the program stops instead of echoing secrets.

Multi-user mode creates one session with the deployment token, then uses the returned student token for capabilities and data. After receiving a valid student token, it attempts to delete that session in `finally`, including after a failed read or cancellation. Cleanup uses a five-second timeout; HTTP 204 and already-expired HTTP 401 count as success. Other cleanup failures are reported and exit nonzero. If cleanup cannot reach the server, the session remains until its idle/maximum expiry or server restart.

Returned detail and attachment IDs belong to this session's discovered links. Because the example deletes its multi-user session on exit, those IDs cannot be reused in a later invocation. See the [API walkthrough](../docs/api.md) to keep one session open while following filters, details, and attachments.

Requests use a 60-second timeout, disable redirects and environment proxies, and never retry automatically. Errors print only safe status/code/request-ID diagnostics. Exit codes are `0` for success, `1` for request/response/cleanup failure, `2` for invalid input or incomplete prompts, and `130` for cancellation. An unavailable resource is a failure and is never requested.
