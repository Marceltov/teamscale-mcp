---
status: accepted
date: 2026-10-16
decision-makers: Marcel Bruckner
consulted: Claude
informed: —
---

# Retry startup until Teamscale is up instead of degrading on the first refused spec download

## Context and Problem Statement

This sidecar downloads Teamscale's OpenAPI spec once, at startup, and builds every
tool from it. Teamscale itself takes minutes to boot; the sidecar takes seconds. When
both are started at the same instant the first download is refused, and `main()`
fell straight back to `build_error_server()`: a server that completes the MCP
handshake, exposes only `startup_error`, and answers `/health` with `503` forever.
Nothing retries, and Docker's restart policy does not act on an unhealthy container,
so the degraded state persists until a human restarts it.

Compose's `depends_on: condition: service_healthy` covers only `docker compose up`.
It does not cover the case that actually bit: a Docker daemon restart, here caused by
an unattended `docker-ce` upgrade on 2026-10-16 at 06:47 local on the host guest,
restores every container at once and ignores compose ordering. The MCP started
3 minutes before Teamscale answered, degraded, and stayed `503` for over three hours
until noticed via Uptime Kuma. The container log shows the same sequence had happened
at least once before.

## Decision Drivers

* A sidecar must survive its upstream being slower to boot, whichever mechanism starts
  the pair.
* The `startup_error` fallback is valuable for a Teamscale that is genuinely
  misconfigured or gone; it should not be reached by a transient ordering race.
* No new dependency, no supervisor, no change to the compose file of any consumer.

## Considered Options

* Retry the whole server build in `main()` for a bounded window, then fall back as
  before.
* Keep the error server but have it retry in the background and swap itself for the
  real server once the spec is available.
* Exit non-zero on failure and rely on Docker's restart policy to retry.

## Decision Outcome

Chosen option: "Retry the whole server build in `main()` for a bounded window",
because it is one function and three tests, covers every transient cause (spec
download, OIDC discovery) rather than only the spec, and keeps the existing fallback
for the permanent case.

`build_server_with_retry()` calls `build_server()` every
`TEAMSCALE_STARTUP_RETRY_INTERVAL` seconds (default 5) until it succeeds or
`TEAMSCALE_STARTUP_TIMEOUT` seconds (default 600) have passed, logging each failed
attempt to stderr, then re-raises the last error so `main()` falls back to the error
server exactly as before. The clock and sleep are injectable, so the loop is unit
tested without waiting.

### Consequences

* Good, because a daemon restart, `compose up`, or a Teamscale upgrade that takes a
  few minutes no longer leaves the MCP degraded.
* Good, because the fallback still exists for a Teamscale that never comes up, and the
  window is tunable per deployment.
* Bad, because nothing listens during the retry window, so the container's health
  check fails for as long as Teamscale takes to boot. That is honest: no tools are
  available yet. Consumers whose health-check `start_period` is shorter than
  Teamscale's boot will see the container flagged unhealthy in that window.
* Bad, because once the window is exhausted the fallback still does not retry; a
  Teamscale that comes up after 10 minutes needs a container restart. Raise
  `TEAMSCALE_STARTUP_TIMEOUT` where that is expected.

### Confirmation

`app/tests/test_startup_retry.py` drives the loop with a fake clock: success after
transient failures, re-raise at the deadline, and env-driven configuration. The
production path is confirmed by restarting both containers together and watching the
MCP log `Startup attempt N failed ... retrying` until Teamscale answers, then `/health`
returning `200`.

## Pros and Cons of the Options

### Background retry inside the error server

* Good, because `/health` would report the degraded state while still self-healing.
* Bad, because swapping a FastMCP instance under a running uvicorn is not a supported
  operation; it would need a proxy layer or a process restart anyway.

### Exit non-zero and let Docker restart the container

* Good, because it is the smallest diff of all.
* Bad, because the `startup_error` fallback, which exists to explain a permanent
  failure over MCP, would never be reachable again.
* Bad, because Docker's restart back-off grows to 1 minute and the restart loop shows
  up as a flapping container in every monitor.

## More Information

Incident that produced this: main-server post-mortem on the 2026-10-16 Kuma outage
(CT 112 `teamscale-mcp` `503` from 06:50 local). The Immich sidecar loss in the same
incident is unrelated to this repo.
