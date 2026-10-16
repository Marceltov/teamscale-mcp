# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone MCP server that turns the [Teamscale](https://teamscale.com) REST API
into MCP tools. It runs as a **container sidecar** next to a Teamscale instance. At
startup it downloads the instance's **live** OpenAPI spec
(`GET {TEAMSCALE_SERVER_URL}/openapi.json`) using bootstrap credentials and turns
every documented operation into a tool via `FastMCP.from_openapi`, served over
streamable **HTTP**. It stores no client secret: each MCP client presents its own
Teamscale identity via `X-Teamscale-User`/`X-Teamscale-Token` headers, which the
server assembles into HTTP Basic auth and forwards per-request to Teamscale.

The entire server is one module: `app/server.py`.

**Relation to the official Teamscale Claude Code plugin
([teamscale/claude-code](https://github.com/teamscale/claude-code), the `ts_agent_helper`
package):** that plugin is a *per-user Claude Code plugin* bundling curated skills plus a
*local stdio* MCP server; this repo is a *central HTTP sidecar* exposing the full REST
surface with per-request credential pass-through. They're complementary — the plugin's
value is its workflows, and this sidecar can serve as the shared REST MCP backend those
workflows point at — replacing **only `ts_agent_helper`** (the per-user Python MCP
backend). The `teamscale-dev` CLI and `.teamscale.toml` stay; they serve local analysis /
pre-commit / project-mapping, which this sidecar doesn't cover. Note `teamscale-dev` is a
**standalone Java application** the user installs separately (not a Python tool, and used
for more than this plugin) — which is why replacing `ts_agent_helper` drops the Python
prerequisite but keeps `teamscale-dev`.
`docs/comparison-to-teamscale-claude-code/README.md` is the canonical write-up (full
comparison + per-skill before/after sequence diagrams); the main README only carries a
short pointer to it. Keep that doc and this note in sync if the framing changes.

## Commands

Development uses `uv` (in `app/`) and Docker Compose (at the repo root).

```bash
# Run the unit test suite (no running Teamscale needed). Extra args pass through
# to pytest.
./run-tests.sh
./run-tests.sh -k test_fetch_spec   # a single test by keyword

# Manual test run (from app/).
cd app && uv run pytest
cd app && uv run pytest -k test_assembles_basic_auth_from_contextvars   # single test

# Dev stack: throwaway Teamscale + MCP built from local source.
docker compose up -d --build     # rebuild after any change under app/
curl http://localhost:8081/health   # -> ok

# Reset the dev Teamscale to the committed clean fixture, then run the suite
# (restores teamscale-backup.zip, re-analyzes, rebuilds the MCP). Args pass to
# pytest, e.g. ./run-tests-clean.sh tests/live -q
./run-tests-clean.sh
```

The image is built and pushed to the GitLab Container Registry
(`registry.gitlab.com/cqse/internal/teamscale-mcp`) by `.gitlab-ci.yml` on push to
`main` and on `v*` tags.

## Test layout

Unit tests (`app/tests/test_*.py`) drive `server.py` through a mock httpx transport —
no running Teamscale required. They cover the bootstrap spec download
(`test_fetch_spec`), the client-identity middleware and Basic-auth assembly
(`test_middleware`, `test_auth`), server construction (`test_build_server`), and the
`startup_error` fallback (`test_error_server`).

Live integration tests (`app/tests/live/`) drive the *running* MCP server over HTTP
via `fastmcp.Client`, presenting the dev identity headers (`X-Teamscale-User: admin`
+ the token from `api.token`) and exercising the real Teamscale REST API behind it.
They **auto-skip** when the MCP `/health` endpoint is unreachable (see
`tests/live/conftest.py` + `_client.stack_reachable`), so `uv run pytest` still passes
with nothing running. They currently cover projects, findings (list, single, count,
descriptions, filtering), and the identity/auth gate (`test_projects`, `test_findings`,
`test_finding_descriptions`, `test_findings_filtering`, `test_auth`) — and grow as more
tools are exercised. Config is overridable via `TEAMSCALE_MCP_URL` /
`TEAMSCALE_MCP_USER` / `TEAMSCALE_MCP_TOKEN` / `TEAMSCALE_MCP_PROJECT`.

## Architecture

`build_server()` in `app/server.py` is the core. It fetches the OpenAPI spec via
`fetch_spec()` (using the bootstrap `TEAMSCALE_SPEC_USER`/`TEAMSCALE_SPEC_TOKEN`
against `{TEAMSCALE_SERVER_URL}/openapi.json?include-internal={TEAMSCALE_INCLUDE_INTERNAL}`),
then calls `FastMCP.from_openapi` on the resulting dict, generating one tool per
Teamscale REST operation.

Tool surface can be trimmed via `build_route_maps()`: `TEAMSCALE_EXCLUDE_TAGS` and
`TEAMSCALE_INCLUDE_TAGS` (both comma-separated) are translated into one
single-tag `RouteMap` per tag, since `RouteMap.tags` matching is a subset check
("all of these tags"), not an OR — each env var is naturally an OR-list of tags, so
each tag needs its own RouteMap. Excludes take precedence; an include allowlist keeps
only matching tags and drops everything else via a trailing catch-all exclude.
`TEAMSCALE_EXCLUDE_METHODS` (also comma-separated) drops operations by HTTP verb;
unlike tags, `RouteMap.methods` is OR-matched, so the whole set goes in one
`RouteMap`. Name-based filtering rides on top via `build_route_map_fn()`, passed as
FastMCP's `route_map_fn` — a callback FastMCP runs *after* the route maps, so
`TEAMSCALE_INCLUDE_NAMES` / `TEAMSCALE_EXCLUDE_NAMES` (matched on `operationId`)
override the tag/method decision. Include wins over exclude when an id is in both.

Also note `validate_output=False`: the live API returns `null` for fields the spec
types as plain strings, so output validation would reject otherwise-successful calls.

`build_server()` also calls `_patch_freeform_map_body()`, which works around a
FastMCP bug for operations whose request body is a **free-form map**
(`{"type": "object", "additionalProperties": {...}}` with no named `properties`,
e.g. `getFindingTypeDescriptions`, `setFindingDueDates`, `updateProjectProperties`).
FastMCP exposes the whole map as one synthetic `body` argument but then forwards it
double-wrapped as `{"body": <map>}`, which Teamscale rejects with
`400 Cannot parse JSON because of mismatched input data`. The patch wraps
`RequestDirector._unflatten_arguments` to strip that wrapper so the map is sent
verbatim; it's guarded (idempotent, silently skipped on any import/internal change)
so a future FastMCP fix or refactor can't break startup. Covered by
`tests/test_map_body.py` (unit) and `tests/live/test_finding_descriptions.py` (live).

### Client identity pass-through (the auth model)

Two tiers of credentials are in play:

1. **Bootstrap** (`TEAMSCALE_SPEC_USER`/`TEAMSCALE_SPEC_TOKEN`, env vars set by the
   deployer): used once, at startup, only to download the OpenAPI spec via HTTP
   Basic. Never used for a tool call made on behalf of a client.
2. **Per-request identity**: `TokenCaptureMiddleware` (pure-ASGI, not
   `BaseHTTPMiddleware`, so it doesn't buffer the streamable-HTTP response) requires
   `X-Teamscale-User` and `X-Teamscale-Token` headers on the MCP path, rejecting
   requests missing either as `401` before FastMCP sees them. It stashes both in
   contextvars. `/health` is always allowed through unauthenticated.
   `TeamscaleBasicAuth` (an `httpx.Auth`) reads those contextvars on the outgoing
   Teamscale call and sets `Authorization: Basic base64(user:token)`.

Validity is enforced by Teamscale when the forwarded request arrives — this server
never validates the credentials itself.

The above is **`headers` mode**, the default and the only mode described so far. A
second mode, **`oauth`** (`TEAMSCALE_AUTH_MODE=oauth`), replaces steps 2 with a
standard MCP OAuth flow instead of client-supplied headers:

The server advertises the standard MCP OAuth flow via FastMCP's `OIDCProxy` (built in
`build_auth()`), proxying login to a generic OIDC issuer reached via
`TEAMSCALE_OIDC_CONFIG_URL` discovery. `OIDCProxy` issues the MCP client its own
reference JWT and stores the upstream IdP token; on each tool call
`get_access_token().token` yields the **upstream** access token, not the raw `/mcp`
`Authorization` header (that carries FastMCP's own non-forwardable reference JWT).
`TeamscaleBearerAuth` forwards that upstream token as `Authorization: Bearer` on the
outgoing Teamscale call. Teamscale validates the JWT itself, in its
Authentication-Proxy / Bearer-Token mode (configured JWKS + username claim) — this
server still validates nothing. In oauth mode `TokenCaptureMiddleware` is not used at
all; FastMCP's own auth middleware gates the `/mcp` path and emits the
spec-correct `401` + `WWW-Authenticate` / Protected-Resource-Metadata. `/health`
stays unauthenticated in both modes.

**Deployment prerequisites for oauth mode (not code in this repo):**

1. Register an OAuth app in the IdP and set `TEAMSCALE_OIDC_CONFIG_URL`,
   `TEAMSCALE_OIDC_CLIENT_ID`, `TEAMSCALE_OIDC_CLIENT_SECRET`, optionally
   `TEAMSCALE_OIDC_AUDIENCE`, and `MCP_BASE_URL` (the externally reachable base URL
   this server is deployed at, used for the OAuth redirect).
2. Configure the IdP to mint **JWT** access tokens for that app, not opaque tokens —
   Teamscale's bearer-token validation needs a JWT it can verify against a JWKS.
3. Configure Teamscale's own Authentication-Proxy / Bearer-Token mode with the IdP's
   JWKS (public key) and the username claim, so IdP usernames map to Teamscale
   usernames. This is Teamscale-side configuration; nothing in this repo does it.

### Startup resilience

`main()` builds the server through `build_server_with_retry()`: any exception from
`build_server()` (spec download refused because Teamscale is still booting, OIDC
discovery failing) is retried every `TEAMSCALE_STARTUP_RETRY_INTERVAL` seconds (default
5) for `TEAMSCALE_STARTUP_TIMEOUT` seconds (default 600). Nothing listens during the
retry window, so the container health check reports failing until an attempt succeeds.
Only once the window is exhausted does `main()` fall back to `build_error_server()`,
which completes the MCP handshake but exposes only a `startup_error` tool describing
the failure — instead of dying with an opaque connection error. The fallback does not
retry; `/health` stays `503` until the container is restarted. See
`adr/0001-retry-startup-until-teamscale-is-up.md` for why (`tests/test_startup_retry`
covers the loop with an injected clock).

### Configuration

All config is environment variables (no CLI args), so the server runs cleanly as a
sidecar: `TEAMSCALE_SERVER_URL`, `TEAMSCALE_SPEC_USER`, `TEAMSCALE_SPEC_TOKEN`,
`TEAMSCALE_INCLUDE_INTERNAL`, `TEAMSCALE_EXCLUDE_TAGS`, `TEAMSCALE_INCLUDE_TAGS`,
`TEAMSCALE_EXCLUDE_METHODS`, `TEAMSCALE_INCLUDE_NAMES`, `TEAMSCALE_EXCLUDE_NAMES`,
`MCP_HOST`, `MCP_PORT`, `MCP_PATH`, `MCP_ALLOWED_HOSTS`. Host protection
(DNS-rebinding) is **off by default** (any Host accepted; the client identity headers
are the real gate) and only restricts when `MCP_ALLOWED_HOSTS` is set.

Auth-mode selection and its oauth-only vars (see the "auth model" section above for
what they do): `TEAMSCALE_AUTH_MODE` (`headers` default, or `oauth`),
`TEAMSCALE_OIDC_CONFIG_URL`, `TEAMSCALE_OIDC_CLIENT_ID`, `TEAMSCALE_OIDC_CLIENT_SECRET`,
`TEAMSCALE_OIDC_AUDIENCE`, `MCP_BASE_URL`. The last five are required only when
`TEAMSCALE_AUTH_MODE=oauth`; `build_auth()` raises at startup if any but
`TEAMSCALE_OIDC_AUDIENCE` (optional) is missing in that mode.

## Dev fixture credentials

The seeded Teamscale at http://localhost:8080 uses HTTP Basic auth `admin:<token>`,
where `<token>` is the committed dev API token in [`api.token`](api.token) — also
used as `TEAMSCALE_SPEC_TOKEN` for the `teamscale-mcp` service in
`docker-compose.yaml`. This is a **committed dev fixture** scoped to the disposable
instance — never reuse it anywhere real. The clean seeded state is committed as
`teamscale-backup.zip` (a Teamscale backup export) so a known fixture can be restored
for testing; the live `teamscale-data/` directory the container uses is untracked.

## Diagrams

PlantUML sources under `docs/` render to PNGs via the `.githooks/pre-commit` hook
(enable once with `git config core.hooksPath .githooks`; requires `plantuml`, plus
ImageMagick `magick` for the before/after merge). The hook recurses through `docs/`,
re-rendering and staging any PNG that is missing or older than its `.puml` or a theme
it includes. Every diagram — top-level and the plugin-comparison set — shares one
theme, `docs/theme.iuml` (the CQSE design system: orange arrows, navy participants,
highlighter-yellow notes, plus box-background and arrow-highlight variables like
`$ws_bg` / `$tool_color`). It is include-only (no `@startuml`) and so is skipped as a
render target. Each diagram renders its PNG **in place** next to its
`.puml`. For a **before/after pair** in a `diagrams/` subfolder, the hook also merges
the two renders into one side-by-side composite (`<name>-before.png` +
`<name>-after.png` → `../<name>.png`) **one level up**, next to the folder README — so
only the merged composites sit beside the README while the per-side renders stay in
`diagrams/`. The top-level diagrams (`architecture`, `sequence-overview`,
`auth-sequence`) are embedded in the main README; the plugin-comparison set lives in
`docs/comparison-to-teamscale-claude-code/` (sources **and** per-side renders in its
`diagrams/`, merged composites beside its README) — see the "What this is" note above.
`docs/regenerate.sh` force-rebuilds **every** diagram under `docs/` (render all +
merge) unconditionally — the whole-repo counterpart to the stale-only pre-commit hook;
`docs/comparison-to-teamscale-claude-code/diagrams/regenerate.sh` does the same for
just that folder.
