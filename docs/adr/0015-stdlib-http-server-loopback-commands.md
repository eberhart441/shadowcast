# 0015. Standard-library HTTP server; commands only from this machine by default

- **Status:** Accepted
- **Date:** 2026-11-07
- **Recorded in:** issue #9 ("Implementation Decisions"), `12bdc48` (server, ticket #12), `a7b0cd7` ("Harden dashboard commands: media type, Host/Origin guard, scalar validation")
- **Related:** [0004](0004-wired-receivers-wireless-deferred.md), [0012](0012-host-ts-line-stream-seam.md), [0016](0016-calibration-off-the-server-lock.md)

## Context

The demo needs one URL that shows the live room, works offline, and runs with or without hardware. Setup had to stay one `pip install` of the existing requirements (issue #9, story 40). The server also takes commands that change thresholds and write files, so a page on another site, or another machine on the network, must not be able to trigger them.

## Decision

- **HTTP on the standard library** (`http.server.ThreadingHTTPServer`), no web framework: `GET /`, `GET /api/state`, `GET /api/events` (server-sent events, at most about 5 per second) and five POST commands. The page is one static HTML file with no network assets.
- **Bind to `127.0.0.1` by default.** `--host` opts into the network.
- **Commands run only on a loopback bind**, or with an explicit `--allow-control`; otherwise they return 403.
- **Every command must also pass these checks:** `Content-Type: application/json` (else 415, which blocks a cross-site form), a `Host` naming this server and its port (else 403, which blocks DNS rebinding) and, when sent, an `Origin` of this server (else 403). Bodies are at most 64 KiB, must be JSON objects, and unknown fields are refused. Replays refuse every command with 409.

## Alternatives considered

- **A web framework.** Ruled out in issue #9: "Standard library only; no web framework".
- **Listen on the network by default.** Rejected (issue #9, story 46: "so that I don't expose the rig by accident").
- **Authentication.** Out of scope for a local demo tool (issue #9, "Out of Scope"). There is none today.

## Consequences

- `requirements.txt` has no web dependency. "Standard library only" describes the HTTP layer; the numeric pipeline behind it uses numpy.
- With `--host 0.0.0.0`, anyone who can reach the port can read the state; with `--allow-control` as well, they can also send commands. Field use needs authentication first ([0004](0004-wired-receivers-wireless-deferred.md)).
- Request validation is hand-written, so it is ours to test: `tests/test_dashboard_guard.py` covers the refusals.
- The event stream is one-way; commands are separate POST requests.

## Evidence

- **The requirement:** issue #9 stories 40 (standard library only), 41 (one static page, no external assets), 46 (localhost by default) and 47 (network on request); "Implementation Decisions", "Backend shape" and "Security".
- **The checks, in code:** `csi/server.py`, `FrameServer.refusal`, `controls_allowed`, `is_loopback`; documented in the README "Security" section.
