import base64
import os
import sys
import time
import traceback
from collections.abc import Callable
from contextvars import ContextVar

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.providers.openapi import MCPType, RouteMap
from fastmcp.utilities.openapi import HTTPRoute
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# Per-request holders for the incoming client identity. Populated by
# TokenCaptureMiddleware (Task 3) and read by TeamscaleBasicAuth on the
# outgoing Teamscale call.
_incoming_user: ContextVar[str | None] = ContextVar("incoming_user", default=None)
_incoming_token: ContextVar[str | None] = ContextVar("incoming_token", default=None)


class TeamscaleBasicAuth(httpx.Auth):
    """Forward the client's identity to Teamscale as HTTP Basic auth.

    The username and API token arrive per-request in contextvars (set by
    TokenCaptureMiddleware). Teamscale authenticates via Basic auth, so we
    assemble `Authorization: Basic base64(user:token)` on every outgoing call.
    """

    def auth_flow(self, request: httpx.Request):
        user = _incoming_user.get()
        token = _incoming_token.get()
        if not user or not token:
            raise RuntimeError(
                "No client X-Teamscale-User/X-Teamscale-Token available for the "
                "Teamscale call."
            )
        raw = base64.b64encode(f"{user}:{token}".encode()).decode()
        request.headers["Authorization"] = f"Basic {raw}"
        yield request


class TeamscaleBearerAuth(httpx.Auth):
    """Forward the OAuth-authenticated user's UPSTREAM access token to Teamscale.

    Used in oauth mode. FastMCP's OIDCProxy validates the client's reference token
    and swaps it for the upstream IdP token, exposed here via get_access_token().token.
    Teamscale then validates that JWT itself (bearer-token mode: signature + username
    claim). We must forward THIS token, not the raw /mcp Authorization header (which
    carries FastMCP's non-forwardable reference JWT).
    """

    def auth_flow(self, request: httpx.Request):
        token = get_access_token()
        if token is None or not getattr(token, "token", None):
            raise RuntimeError(
                "No authenticated access token available for the Teamscale call "
                "(oauth mode)."
            )
        request.headers["Authorization"] = f"Bearer {token.token}"
        yield request


def _is_freeform_map_body(schema: object) -> bool:
    """True when a request body schema is a free-form map: an object with
    ``additionalProperties`` but no named ``properties`` (e.g. Teamscale's
    ``Map<String, Set<String>>`` bodies)."""
    return (
        isinstance(schema, dict)
        and schema.get("type") == "object"
        and not schema.get("properties")
        and "additionalProperties" in schema
    )


def _patch_freeform_map_body() -> None:
    """Fix FastMCP's double-wrapping of free-form map request bodies.

    FastMCP's OpenAPI provider represents a whole-body free-form map as a single
    synthetic ``body`` argument, but its RequestDirector then reconstructs the
    outgoing body as ``{"body": <the map>}`` (because the body schema's type is
    "object"), instead of sending the map itself. Teamscale rejects that shape
    with ``400 Cannot parse JSON because of mismatched input data``. This affects
    every operation with a ``Map`` body (getFindingTypeDescriptions,
    setFindingDueDates, updateProjectProperties, ...).

    We wrap ``RequestDirector._unflatten_arguments`` so that, for a free-form map
    body, the single synthetic wrapper key is stripped and the map is forwarded
    verbatim. Guarded so a future FastMCP that fixes this (or changes internals)
    doesn't break startup: on any error the patch is silently skipped.
    """
    try:
        from fastmcp.utilities.openapi.director import RequestDirector
    except Exception:  # pragma: no cover - defensive against internal churn
        return

    original = RequestDirector._unflatten_arguments
    if getattr(original, "_teamscale_map_body_patched", False):
        return

    def patched(self, route, flat_args):
        path_p, query_p, header_p, cookie_p, body = original(self, route, flat_args)
        request_body = getattr(route, "request_body", None)
        content_schema = getattr(request_body, "content_schema", None)
        if (
            content_schema
            and isinstance(body, dict)
            and len(body) == 1
            and _is_freeform_map_body(next(iter(content_schema.values())))
        ):
            # The lone key is FastMCP's synthetic body wrapper; the map the
            # client supplied is its value. Forward that value directly.
            body = next(iter(body.values()))
        return path_p, query_p, header_p, cookie_p, body

    patched._teamscale_map_body_patched = True
    RequestDirector._unflatten_arguments = patched


HEALTH_PATH = "/health"

_UNAUTHORIZED_BODY = b'{"error":"missing X-Teamscale-User/X-Teamscale-Token header"}'


class TokenCaptureMiddleware:
    """Pure-ASGI middleware that requires the client identity headers on the
    MCP endpoint and stashes them for the outgoing Teamscale call.

    Implemented at the ASGI layer (not BaseHTTPMiddleware) so it does not buffer
    the streamable-HTTP response. Requests missing either X-Teamscale-User or
    X-Teamscale-Token are rejected with 401 before FastMCP sees them; validity
    is enforced by Teamscale on the actual call. The health check is always
    allowed through unauthenticated.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        user = headers.get(b"x-teamscale-user", b"").decode()
        token = headers.get(b"x-teamscale-token", b"").decode()
        if not user or not token:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
            return
        user_tok = _incoming_user.set(user)
        token_tok = _incoming_token.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            _incoming_user.reset(user_tok)
            _incoming_token.reset(token_tok)


def fetch_spec(
    server_url: str,
    user: str,
    token: str,
    include_internal: bool = False,
    *,
    client: httpx.Client | None = None,
) -> dict:
    """Download the Teamscale OpenAPI spec at startup using bootstrap creds.

    The spec is version-specific (its paths embed the instance version), so it
    is fetched live rather than bundled. `user`/`token` are the deployer's
    technical account, used ONLY for this download -- never for tool calls.
    """
    url = f"{server_url.rstrip('/')}/openapi.json"
    params = {"include-internal": "true" if include_internal else "false"}
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=30)
    try:
        response = client.get(url, params=params, auth=(user, token))
        response.raise_for_status()
        spec = response.json()
    except Exception as e:
        raise RuntimeError(
            f"Failed to download Teamscale OpenAPI spec from {url}: {e}"
        ) from e
    finally:
        if owns_client:
            client.close()
    if not isinstance(spec, dict):
        raise RuntimeError(f"Teamscale OpenAPI spec at {url} is not a JSON object.")
    return spec


SERVER_ENV = "TEAMSCALE_SERVER_URL"
SPEC_USER_ENV = "TEAMSCALE_SPEC_USER"
SPEC_TOKEN_ENV = "TEAMSCALE_SPEC_TOKEN"
INCLUDE_INTERNAL_ENV = "TEAMSCALE_INCLUDE_INTERNAL"
EXCLUDE_TAGS_ENV = "TEAMSCALE_EXCLUDE_TAGS"
INCLUDE_TAGS_ENV = "TEAMSCALE_INCLUDE_TAGS"
EXCLUDE_METHODS_ENV = "TEAMSCALE_EXCLUDE_METHODS"
INCLUDE_NAMES_ENV = "TEAMSCALE_INCLUDE_NAMES"
EXCLUDE_NAMES_ENV = "TEAMSCALE_EXCLUDE_NAMES"

AUTH_MODE_ENV = "TEAMSCALE_AUTH_MODE"
OIDC_CONFIG_URL_ENV = "TEAMSCALE_OIDC_CONFIG_URL"
OIDC_CLIENT_ID_ENV = "TEAMSCALE_OIDC_CLIENT_ID"
OIDC_CLIENT_SECRET_ENV = "TEAMSCALE_OIDC_CLIENT_SECRET"
OIDC_AUDIENCE_ENV = "TEAMSCALE_OIDC_AUDIENCE"
MCP_BASE_URL_ENV = "MCP_BASE_URL"

AUTH_MODE_HEADERS = "headers"
AUTH_MODE_OAUTH = "oauth"

DEFAULT_SERVER_URL = "http://teamscale:8080"

_TRUE_VALUES = {"1", "true", "yes", "on"}


def env_set(name: str) -> set[str]:
    """Parse a comma-separated env var into a set of values (empty if unset).

    Used for the tag, method, and operation-name filter env vars.
    """
    raw = os.environ.get(name, "").strip()
    return {t.strip() for t in raw.split(",") if t.strip()}


def auth_mode() -> str:
    """Return the configured client-auth mode: 'headers' (default) or 'oauth'.

    'headers' keeps the legacy X-Teamscale-User/Token -> HTTP Basic pass-through.
    'oauth' turns on the standard MCP OAuth flow (OIDCProxy + JWT forwarding).
    """
    mode = os.environ.get(AUTH_MODE_ENV, AUTH_MODE_HEADERS).strip().lower()
    if mode not in (AUTH_MODE_HEADERS, AUTH_MODE_OAUTH):
        raise RuntimeError(
            f"{AUTH_MODE_ENV} must be '{AUTH_MODE_HEADERS}' or '{AUTH_MODE_OAUTH}', "
            f"got '{mode}'."
        )
    return mode


def build_auth() -> OIDCProxy | None:
    """Build the FastMCP server auth provider for the current mode.

    Returns None in headers mode (FastMCP auth off; TokenCaptureMiddleware gates).
    In oauth mode returns an OIDCProxy wired from env. verify_id_token is left False
    so the *access* token (the one forwarded to Teamscale) is what gets validated.
    NOTE: constructing OIDCProxy performs an OIDC discovery request against
    TEAMSCALE_OIDC_CONFIG_URL.
    """
    if auth_mode() != AUTH_MODE_OAUTH:
        return None
    required = {
        OIDC_CONFIG_URL_ENV: os.environ.get(OIDC_CONFIG_URL_ENV),
        OIDC_CLIENT_ID_ENV: os.environ.get(OIDC_CLIENT_ID_ENV),
        OIDC_CLIENT_SECRET_ENV: os.environ.get(OIDC_CLIENT_SECRET_ENV),
        MCP_BASE_URL_ENV: os.environ.get(MCP_BASE_URL_ENV),
    }
    missing = sorted(name for name, val in required.items() if not val)
    if missing:
        raise RuntimeError(
            f"{AUTH_MODE_ENV}={AUTH_MODE_OAUTH} requires these env vars: "
            f"{', '.join(missing)}."
        )
    return OIDCProxy(
        config_url=required[OIDC_CONFIG_URL_ENV],
        client_id=required[OIDC_CLIENT_ID_ENV],
        client_secret=required[OIDC_CLIENT_SECRET_ENV],
        audience=os.environ.get(OIDC_AUDIENCE_ENV) or None,
        base_url=required[MCP_BASE_URL_ENV],
        verify_id_token=False,
    )


def build_route_maps(
    include_tags: set[str], exclude_tags: set[str], exclude_methods: set[str]
) -> list[RouteMap]:
    """Map the tag/method env vars to FastMCP RouteMaps (first match wins).

    Excludes take precedence; an include allowlist keeps only matching tags and
    drops everything else via a trailing catch-all exclude.

    NOTE: `RouteMap.tags` is matched as "all of these tags must be present on
    the route" (a subset check), not "any of these tags" -- see
    fastmcp.server.providers.openapi.routing._determine_route_type. A
    comma-separated env var is naturally an OR-list ("exclude anything tagged
    Backup OR System"), so each tag gets its own single-tag RouteMap rather
    than passing the whole set to one RouteMap (which would require a route to
    carry *every* listed tag simultaneously to match). `RouteMap.methods`, by
    contrast, IS OR-matched (`route.method in methods`), so one RouteMap carries
    the whole method set.
    """
    maps: list[RouteMap] = []
    if exclude_methods:
        maps.append(
            RouteMap(methods=sorted(exclude_methods), mcp_type=MCPType.EXCLUDE)
        )
    for tag in exclude_tags:
        maps.append(RouteMap(tags={tag}, mcp_type=MCPType.EXCLUDE))
    if include_tags:
        for tag in include_tags:
            maps.append(RouteMap(tags={tag}, mcp_type=MCPType.TOOL))
        maps.append(RouteMap(mcp_type=MCPType.EXCLUDE))
    return maps


def build_route_map_fn(
    include_names: set[str], exclude_names: set[str]
) -> Callable[[HTTPRoute, MCPType], MCPType | None] | None:
    """Name-based override applied after build_route_maps (see build_server).

    FastMCP runs this callback per route *after* the RouteMaps decide, so it
    can override that decision. `TEAMSCALE_INCLUDE_NAMES` forces an operation
    back in (overriding any method/tag exclude or the allowlist catch-all);
    `TEAMSCALE_EXCLUDE_NAMES` forces one out. Matching is on operationId and is
    case-sensitive. Include wins when an id appears in both. Returns None when
    neither set is populated, so no callback is wired up.
    """
    if not include_names and not exclude_names:
        return None

    def override(route: HTTPRoute, current_type: MCPType) -> MCPType | None:
        if route.operation_id in include_names:
            return MCPType.TOOL
        if route.operation_id in exclude_names:
            return MCPType.EXCLUDE
        return None

    return override


def register_health(mcp: FastMCP, *, healthy: bool = True) -> None:
    """Add an unauthenticated health endpoint for container healthchecks.

    `healthy=False` is used by `build_error_server` so the container
    healthcheck reports unhealthy when startup failed, instead of reporting
    "ok" for a server that exposes no real Teamscale tools.
    """

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(_request: Request):
        if healthy:
            return PlainTextResponse("ok")
        return PlainTextResponse(
            "startup failed: Teamscale OpenAPI spec unavailable", status_code=503
        )


def build_server(
    client: httpx.AsyncClient | None = None, spec: dict | None = None
) -> FastMCP:
    """Fetch the Teamscale OpenAPI spec (unless injected) and turn every
    documented endpoint into a FastMCP tool. Per-request client identity is
    supplied by the client via headers (see TokenCaptureMiddleware /
    TeamscaleBasicAuth), so no client credential is read here.

    `client`/`spec` are injectable for testing; in production the client targets
    TEAMSCALE_SERVER_URL and authenticates from the per-request contextvars, and
    the spec is downloaded with the bootstrap creds.
    """
    _patch_freeform_map_body()

    server_url = os.environ.get(SERVER_ENV, DEFAULT_SERVER_URL).rstrip("/")

    if spec is None:
        user = os.environ.get(SPEC_USER_ENV)
        token = os.environ.get(SPEC_TOKEN_ENV)
        if not user or not token:
            raise RuntimeError(
                f"{SPEC_USER_ENV} and {SPEC_TOKEN_ENV} must be set so the server "
                f"can download the Teamscale OpenAPI spec at startup."
            )
        include_internal = (
            os.environ.get(INCLUDE_INTERNAL_ENV, "false").strip().lower()
            in _TRUE_VALUES
        )
        spec = fetch_spec(server_url, user, token, include_internal)

    mode = auth_mode()
    # The outgoing auth depends on the mode: forward the client's OAuth JWT
    # (oauth) or assemble HTTP Basic from the X-Teamscale-* headers (headers).
    outgoing_auth = (
        TeamscaleBearerAuth() if mode == AUTH_MODE_OAUTH else TeamscaleBasicAuth()
    )

    if client is None:
        # Teamscale spec paths are absolute (they embed the version), so the
        # base_url is the bare server URL with no path suffix.
        client = httpx.AsyncClient(
            base_url=server_url, auth=outgoing_auth, timeout=60
        )
    elif client.auth is None:
        # An injected client (tests only today) receives the mode-appropriate
        # auth, but a caller that pre-configured its own auth is left untouched.
        client.auth = outgoing_auth

    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="Teamscale MCP",
        # In oauth mode this installs FastMCP's OAuth/OIDC auth; None in headers mode.
        auth=build_auth(),
        # The live API returns null for fields the spec types as plain strings,
        # so output validation would reject otherwise-successful calls.
        validate_output=False,
        route_maps=build_route_maps(
            env_set(INCLUDE_TAGS_ENV),
            env_set(EXCLUDE_TAGS_ENV),
            {m.upper() for m in env_set(EXCLUDE_METHODS_ENV)},
        ),
        route_map_fn=build_route_map_fn(
            env_set(INCLUDE_NAMES_ENV),
            env_set(EXCLUDE_NAMES_ENV),
        ),
    )
    register_health(mcp)
    return mcp


MCP_HOST_ENV = "MCP_HOST"
MCP_PORT_ENV = "MCP_PORT"
MCP_PATH_ENV = "MCP_PATH"
MCP_ALLOWED_HOSTS_ENV = "MCP_ALLOWED_HOSTS"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8081
DEFAULT_PATH = "/mcp"


def build_error_server(error: BaseException) -> FastMCP:
    """Stand-in MCP server that reports a startup failure over a live
    connection instead of dying with an opaque error (e.g. the spec download
    failed or the instance was unreachable at startup).
    """
    summary = str(error).strip() or error.__class__.__name__
    detail = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    instructions = (
        f"This Teamscale MCP server FAILED TO START and exposes no Teamscale "
        f"tools.\n\nReason: {summary}\n\nThe OpenAPI spec could not be downloaded "
        f"or built. Call the `startup_error` tool for the full error."
    )
    mcp = FastMCP(name="Teamscale MCP (startup failed)", instructions=instructions)
    register_health(mcp, healthy=False)

    @mcp.tool
    def startup_error() -> str:
        """Explain why this Teamscale MCP server failed to start."""
        return (
            "The Teamscale MCP server failed to start, so no Teamscale tools are "
            f"available.\n\n--- Full error ---\n{detail}"
        )

    return mcp


def build_asgi_app(mcp: FastMCP, *, force_token_gate: bool = False) -> object:
    """Build the ASGI app for the current auth mode.

    headers mode: wrap FastMCP's http_app in TokenCaptureMiddleware, which requires
    and captures the X-Teamscale-* identity headers. oauth mode: return http_app
    unwrapped -- FastMCP's own auth middleware validates the bearer and emits the
    spec-correct 401 + Protected-Resource-Metadata. /health stays unauthenticated in
    both modes.

    `force_token_gate=True` overrides the oauth-mode no-wrap behavior and always
    applies TokenCaptureMiddleware. This is for `build_error_server`'s stand-in:
    when startup itself failed, that server's own OAuth provider was never built
    (mcp.auth is None, so FastMCP installs no auth middleware), which would
    otherwise leave the error server -- and the full traceback its `startup_error`
    tool returns -- reachable unauthenticated in oauth mode. Header-gating it is
    the only auth we can still apply.
    """
    path = os.environ.get(MCP_PATH_ENV, DEFAULT_PATH)
    allowed = os.environ.get(MCP_ALLOWED_HOSTS_ENV, "").strip()
    if allowed:
        hosts = [h.strip() for h in allowed.split(",") if h.strip()]
        inner = mcp.http_app(path=path, allowed_hosts=hosts)
        print(f"Host protection ON; allowed hosts (plus localhost): {hosts}",
              file=sys.stderr)
    else:
        inner = mcp.http_app(path=path, host_origin_protection=False)
        print(f"Host protection OFF (any Host accepted) -- set "
              f"{MCP_ALLOWED_HOSTS_ENV} to restrict.", file=sys.stderr)

    if not force_token_gate and auth_mode() == AUTH_MODE_OAUTH:
        return inner
    return TokenCaptureMiddleware(inner)


def serve(mcp: FastMCP, *, force_token_gate: bool = False) -> None:
    """Serve an MCP server over streamable HTTP using the MCP_* env configuration."""
    host = os.environ.get(MCP_HOST_ENV, DEFAULT_HOST)
    port = int(os.environ.get(MCP_PORT_ENV, DEFAULT_PORT))
    path = os.environ.get(MCP_PATH_ENV, DEFAULT_PATH)

    app = build_asgi_app(mcp, force_token_gate=force_token_gate)

    # force_token_gate means we fell back to the header gate regardless of mode
    # (the startup error server), so report that rather than the OAuth flow.
    if auth_mode() == AUTH_MODE_OAUTH and not force_token_gate:
        identity = "client completes the OAuth flow (Bearer token)"
    else:
        identity = "client supplies X-Teamscale-User / X-Teamscale-Token headers"
    print(f"Serving Teamscale MCP on http://{host}:{port}{path} ({identity})",
          file=sys.stderr)
    uvicorn.run(app, host=host, port=port)


STARTUP_TIMEOUT_ENV = "TEAMSCALE_STARTUP_TIMEOUT"
STARTUP_RETRY_INTERVAL_ENV = "TEAMSCALE_STARTUP_RETRY_INTERVAL"
DEFAULT_STARTUP_TIMEOUT = 600.0
DEFAULT_STARTUP_RETRY_INTERVAL = 5.0


def build_server_with_retry(
    build: Callable[[], FastMCP] = build_server,
    *,
    timeout: float | None = None,
    interval: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> FastMCP:
    """Call `build` until it succeeds or `timeout` seconds have elapsed.

    Teamscale boots far slower than this sidecar, so when both are started at
    once (compose up, a Docker daemon restart) the first spec download sees
    "connection refused". Rather than giving up on one attempt and serving the
    `startup_error` fallback until someone restarts the container, keep trying
    every `interval` seconds for `timeout` seconds. The last error is re-raised
    once the deadline passes, so the fallback still applies to a Teamscale
    that never comes up. Both knobs are env-configurable; `sleep`/`clock` are
    injectable for tests.
    """
    if timeout is None:
        timeout = float(os.environ.get(STARTUP_TIMEOUT_ENV, DEFAULT_STARTUP_TIMEOUT))
    if interval is None:
        interval = float(
            os.environ.get(STARTUP_RETRY_INTERVAL_ENV, DEFAULT_STARTUP_RETRY_INTERVAL)
        )
    deadline = clock() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            return build()
        except Exception as e:
            if clock() >= deadline:
                raise
            print(
                f"Startup attempt {attempt} failed: {e} -- retrying in {interval:g}s "
                f"(giving up after {timeout:g}s total)",
                file=sys.stderr,
            )
            sleep(interval)


def main():
    try:
        mcp = build_server_with_retry()
    except Exception as e:
        print(f"Error: failed to build Teamscale MCP server: {e}", file=sys.stderr)
        serve(build_error_server(e), force_token_gate=True)
        return
    serve(mcp)


if __name__ == "__main__":
    main()
