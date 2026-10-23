# cloudflare-dns-autoupdate

Keep DNS records in one or more Cloudflare zones synchronized with values computed on your host.
The service runs a reconciliation cycle immediately, then on a fixed interval. It creates missing
records, patches changed fields, and leaves matching records alone. Zones and records are processed
concurrently; individual resolver and API failures are isolated and logged.

Requires Python 3.13 or newer. The project uses [uv](https://docs.astral.sh/uv/).

## Quick start

Create a Cloudflare API token with **Zone:Read** and **DNS:Edit**, restricted to the zones you intend
to manage. Token verification is informational: some account-owned tokens cannot use the user-token
verification endpoint and still work for DNS operations.

```bash
uv sync
cp config.example.yml config.yml
# Edit config.yml for your zones and records.
export CLOUDFLARE_TOKEN='your-token'
uv run cloudflare-dns-autoupdate --once --dry-run
uv run cloudflare-dns-autoupdate
```

`uv run python -m cloudflare_dns_autoupdate` works too. `--once` runs one cycle without a watcher and
exits with status 1 if any record failed, otherwise 0. Skipped records do not count as failures.
`--dry-run` reads Cloudflare state and resolves content, then logs planned writes without sending
them. It still executes configured shell and Python code.

## Docker

```bash
mkdir -p config
cp config.example.yml config/config.yml
# Edit config/config.yml before starting.
docker build -t cloudflare-dns-autoupdate .
docker run -d --name cfda --restart unless-stopped \
  -e CLOUDFLARE_TOKEN=xxxx \
  -v "$(pwd)/config:/config:ro" \
  cloudflare-dns-autoupdate
docker logs -f cfda
```

Mount the **directory containing `config.yml`**, as shown above. Single-file bind mounts keep
pointing at the old inode when an editor saves atomically, which prevents hot reload from seeing
the replacement file. If file notifications are unavailable on Docker Desktop or network
filesystems, set `WATCHFILES_FORCE_POLLING=true`.

An equivalent `docker-compose.yml`:

```yaml
services:
  cfda:
    build: .
    restart: unless-stopped
    environment:
      CLOUDFLARE_TOKEN: ${CLOUDFLARE_TOKEN}
      LOG_LEVEL: INFO
      LOG_FORMAT: text
      # WATCHFILES_FORCE_POLLING: "true"
    volumes:
      - ./config:/config:ro
```

Export `CLOUDFLARE_TOKEN`, then run `docker compose up -d --build`. The image runs as UID/GID 10001;
the config directory and file must be readable by that user. It includes `/bin/sh`, `curl`, and
`dig` for shell resolvers. `tini` forwards signals and reaps child processes. `docker stop` allows
the current cycle up to ten seconds to finish before cancellation.

To inspect one cycle in a container:

```bash
docker run --rm -e CLOUDFLARE_TOKEN \
  -v "$(pwd)/config:/config:ro" cloudflare-dns-autoupdate --once --dry-run
```

## Configuration reference

The complete commented example is [config.example.yml](config.example.yml).
Unknown keys are errors. Configuration is validated at startup, and startup errors exit with
status 1. Names are fully qualified, normalized to lowercase, and may have one trailing dot.

```yaml
interval: 5m
zones:
  - domain: example.com
    dns:
      - name: home.example.com
        type: A
        content:
          shell: curl -fsS --max-time 10 https://api.ipify.org
        ttl: Auto
        tags: [service:ddns]
        comment: Managed by cloudflare-dns-autoupdate
```

| Top-level field | Default | Meaning |
| --- | --- | --- |
| `token` | Environment fallback | Optional nonempty Cloudflare API token. Stored as a secret. |
| `interval` | `5m` | Integer seconds, a digit string, or whole positive unit segments; minimum 10 seconds. |
| `zones` | Required | Nonempty list of zones; normalized domains must be unique. |

Token resolution uses a nonempty, whitespace-stripped config `token` first, then the
`CLOUDFLARE_TOKEN` environment variable. Missing credentials are a config error. If both are set,
the config value takes precedence and a warning is logged. Token values are redacted in logs;
neither authorization headers nor token values are intentionally logged. `config.yml` is ignored
by Git and excluded from the Docker build context.

Intervals accept `s`, `sec`, `secs`, `second`, `seconds`; `m`, `min`, `mins`, `minute`, `minutes`;
`h`, `hr`, `hrs`, `hour`, `hours`; and `d`, `day`, `days`, case-insensitively. Examples:
`300`, `"300"`, `30s`, `1h30m`, `1d 12h`, `90 minutes`. Spaces between numbers and units are
accepted. A unit may appear only once, including aliases. Fractions, zero-valued segments,
negative values, and intervals below ten seconds are rejected.

| Zone field | Meaning |
| --- | --- |
| `domain` | Required zone apex, such as `example.com`. |
| `dns` | Required nonempty list of records. Each `(name, type)` pair must be unique in the zone. |

| Record field | Default | Meaning |
| --- | --- | --- |
| `name` | Required | Fully qualified name equal to, or below, the zone apex. A leading `*.` wildcard is allowed. |
| `type` | Required | `A`, `AAAA`, `CNAME`, `TXT`, `NS`, or `PTR`; input is case-insensitive. |
| `content` | Required | An object with exactly one string key: `ip`, `shell`, or `eval`. |
| `ttl` | `Auto` | Case-insensitive `Auto` maps to Cloudflare TTL 1; otherwise an integer from 30 to 86400. |
| `tags` | `[]` | Unique strings in `name` or `name:value` form. Comparison ignores order. |
| `comment` | `""` | A string of at most 100 characters. |

TTLs from 30 to 59 are accepted with a warning because they require an Enterprise zone. If an
existing record is proxied, Cloudflare forces Auto TTL: a requested numeric TTL is logged and
excluded from the diff. Empty comments and tags clear those fields on existing records.

### Content sources

Exactly one content source is required:

- `ip`: a static scalar string. For `A`/`AAAA` it is an IP; for `CNAME`/`NS`/`PTR` it is a
  hostname; for `TXT` it is free text. Static values are validated when loading the config.
- `shell`: trusted `/bin/sh -c` code. Stripped UTF-8 stdout is the result. A nonzero exit, empty
  output, or timeout fails the record. Stderr from a successful command produces a warning.
  The environment includes `CFDA_RECORD_NAME`, `CFDA_RECORD_TYPE`, and `CFDA_ZONE`.
- `eval`: trusted Python. An expression's value is the result; a multi-statement script must
  assign `result`. Awaitable results are awaited. `None` and empty results fail the record.
  Context variables are `record_name`, `record_type`, and `zone` (the domain string). Full builtins
  and the modules `os`, `re`, `json`, `socket`, `ipaddress`, and `httpx` are available. Every
  evaluation uses a fresh namespace.

An asynchronous Python resolver example:

```yaml
content:
  eval: |
    async def public_ip():
        async with httpx.AsyncClient() as client:
            response = await client.get("https://api.ipify.org", timeout=10)
            response.raise_for_status()
            return response.text
    result = public_ip()
```

Shell and Python resolutions have a 30-second timeout. A timed-out or cancelled shell has its
whole process group killed and reaped. Synchronous Python executes in a worker thread so it
does not block the event loop. **A timed-out Python thread cannot be killed** and may continue
running in the background; Python's interpreter shutdown may also wait for it. Use explicit
timeouts inside blocking libraries.

Identical `(kind, source text)` sources run once per cycle, sharing successes and failures across
all zones. If a shared source refers to record or zone context, it uses the context of the first
record that starts it; make source text distinct when different contexts must produce different
values. At most eight sources execute at once. Each record's type is validated separately after
resolution, and the cache is discarded after the cycle.

| Type | Validation and normalization |
| --- | --- |
| `A` | IPv4 address, normalized by Python `ipaddress`. |
| `AAAA` | IPv6 address, normalized to compressed form. |
| `CNAME`, `NS`, `PTR` | Hostname with 1–63 character labels using letters, digits, `-`, or `_`; no leading/trailing `-`; total length ≤253. Lowercase with trailing dot removed. |
| `TXT` | Nonempty text of at most 2048 characters, preserved as provided. Shell/eval results are stripped first. |

Dynamic `A`/`AAAA` values must be globally routable. Private, loopback, link-local, unspecified,
multicast, and reserved/documentation addresses are rejected. Static `ip` values may intentionally
use private addresses. Invalid content is never sent to Cloudflare. Existing TXT content with one
pair of surrounding double quotes compares equal to the corresponding unquoted text.

### Trusted code and security

**Configuration is trusted executable code.** `shell` and `eval` intentionally allow arbitrary
code execution with the service user's privileges and environment, including access to its
credentials. There is no sandbox. Protect config write access and review resolver code. Debug
logs include raw resolver output and source snippets, which can contain sensitive data beyond
the redacted Cloudflare token.

## Hot reload and scheduling

The parent config directory is watched, supporting atomic editor saves and Kubernetes ConfigMap
symlink swaps. Events are debounced and SHA-256 hashes ignore unchanged bytes. Valid changes log
added/removed zones and records, modified records, interval changes, and whether the token changed.
Invalid edits or a deleted file retain the previous configuration. A recreated file is picked up.

Each cycle holds a configuration snapshot. Accepted reloads are applied at the cycle boundary;
token changes replace and verify the HTTP client, all reloads clear the zone cache, and the next
cycle runs immediately with a reset schedule. Cycles never overlap. Scheduling is fixed-rate from
cycle start; overruns are logged and the next cycle starts immediately.

SIGTERM/SIGINT stop the watcher and allow an active cycle up to ten seconds to finish, then cancel
it and close HTTP connections. A second signal forces immediate process exit.

## CLI and logging

```text
cloudflare-dns-autoupdate [--config PATH] [--once] [--dry-run]
                          [--log-level LEVEL] [--log-format {text,json}] [--version]
```

| Flag | Environment | Default |
| --- | --- | --- |
| `--config` | `CONFIG_PATH` | `./config.yml` |
| `--once` | — | Off |
| `--dry-run` | `DRY_RUN=true` or `DRY_RUN=1` | Off |
| `--log-level` | `LOG_LEVEL` | `INFO` (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `--log-format` | `LOG_FORMAT` | `text` (`text`, `json`) |

Flags take precedence over environment defaults. Logs go to stdout with millisecond UTC
timestamps and cycle/zone/record context. JSON emits one object per line with `timestamp`, `level`,
`logger`, `message`, and context fields. Logs cover startup, API requests and retries, resolver
decisions, diffs and writes, cycle totals, next scheduled run, reloads, and shutdown. Raw resolver
output is logged at DEBUG; INFO shows validated values. HTTP requests are limited to four at once,
with up to three attempts for network errors, HTTP 429, or 5xx and exponential backoff with jitter.
Numeric `Retry-After` headers are honored up to 60 seconds.

## Managed scope

Only configured names and types are considered. Multiple existing records with the same name and
type are ambiguous and skipped with their IDs and contents logged. A missing or inaccessible zone
is skipped. Other record, zone, and API failures are isolated and summarized per cycle.

The service never deletes records, including those removed from the config. It never sends PUT
requests or manages `proxied`. Updates PATCH only changed content, TTL, comment, and tags, preserving
all unmanaged fields. MX, SRV, CAA, and other composite record types are unsupported. One token
serves all configured zones. There is no HTTP server, health endpoint, or metrics endpoint.

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy --strict src
```

Tests use local subprocesses and mocked Cloudflare HTTP responses with `respx`; no real token or
network access is needed. `CLOUDFLARE_API_BASE_URL` can override the API URL for local tests only.
Runtime dependencies are limited to Pydantic, HTTPX, PyYAML, and watchfiles. Dependency versions
are recorded in the committed `uv.lock`.

Licensed under [GPL-3.0](LICENSE).
