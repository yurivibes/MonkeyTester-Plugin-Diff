# Security — MonkeyTester-Plugin-Diff

This is the `DIFF` test plugin for MonkeyTester. This page says what it
protects, how, and what it does not. It makes **no "zero vulnerabilities"
claim**: it closes the known holes and keeps the attack surface small for its
intended setting.

## Threat model

- **Setting:** a home lab LAN, one user, the plugin and the MonkeyTester viewer
  on the same Linux host. The viewer reaches the plugin at `http://127.0.0.1:8095`.
- **What an attacker on the LAN must not be able to do:** run comparisons or
  upload files through the API, read or write files on the host, run commands,
  exhaust the disk or memory with crafted uploads, or learn secrets.
- **Out of scope:** other local users on the same host, attackers who already
  have the user's shell.

## Network exposure

| Listener | Port | Default bind | Auth |
|---|---|---|---|
| Plugin API | :8095 | `127.0.0.1` | `X-Plugin-Token` (mandatory off loopback) |

The bind/token rules are the same as the other MonkeyTester plugins
(MonkeyTester-Plugin-Esp32, MonkeyTester-Plugin-Server).

## Token setup

- `PLUGIN_BIND` defaults to `127.0.0.1`. Loopback (`127.0.0.0/8`, `::1`,
  `localhost`): the token is optional.
- Any other bind (a LAN address, `0.0.0.0`, `::`): `PLUGIN_TOKEN` is
  **mandatory** and must be at least 16 characters. Without it the plugin prints
  the reason to stderr and **exits with code 2**.
- Generate one with `openssl rand -hex 32`. Put it in a systemd drop-in
  (`systemctl --user edit monkeytester-diff`: `Environment=PLUGIN_BIND=…` and
  `Environment=PLUGIN_TOKEN=…`) or the LaunchAgent's `EnvironmentVariables`.
- When a token is set (on any bind), every request except `GET /health` needs
  the `X-Plugin-Token` header; it is compared with `hmac.compare_digest`
  (constant time). Wrong or missing → 401.
- `GET /health` stays unauthenticated (the viewer's liveness check). It returns
  the contract, plugin name/version, device count (0), capabilities and
  `"docs": "/docs"` while README.md is readable; the host name only to
  authenticated callers. Never the token, the environment, the upload directory
  or configuration.
- `GET /docs` (README.md rendered to HTML) is token-protected like the other
  routes. The renderer (`docs_page.py`) escapes all raw HTML and keeps only
  `http(s)`, `mailto:` and `#` links; the page is served with
  `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; …`,
  `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`.
- The token is stored in **plaintext at rest** on both sides (env/unit drop-in
  here, the viewer's settings file there).

## Input hardening

- **Request size:** JSON bodies are capped at 8 MiB (an element list can be
  long), `POST /uploads` at 64 MiB → 413. A body needs a `Content-Length`
  (chunked → 411); a bad length → 400. Malformed JSON, bad UTF-8 or absurd
  nesting → 400. `timeout_ms` is capped at one hour. Each connection has a
  30 s socket timeout.
- **No tracebacks to clients:** an internal error answers
  `internal plugin error; see the plugin's log`; the traceback goes to the
  plugin's stderr/journal only (also for crashing jobs, whose log and error
  carry only the exception type).
- **Identifiers:** job ids must look like `j-<12 hex>`, upload refs like
  `upload:<12 hex>`; anything else is 404/400.
- **No host paths:** `path` params accept only `upload:<id>` refs. The plugin
  never opens a file a request names.
- **Uploads:** the kind is read from the bytes (PNG magic or zip). A zip is
  validated **before anything is written**: every member must match
  `snapshots/<android|ios>/<W>x<H>@<scale>x/<name>.png` (MonkeyTester's
  `SNAPSHOT_PATH_RE`) or be a directory leading to one; NUL, backslashes,
  absolute paths, `.`/`..` segments and symlink entries refuse the whole upload
  (400), and each target is re-checked with `realpath` + `commonpath`. Each
  member must be a non-empty PNG of at most 8 MiB; at most 1000 files, 4000
  entries and 256 MiB unpacked, counted while inflating (not trusted from the
  headers) → 413. A refused upload leaves nothing on disk. The upload root is
  created `0700` and must be a real directory owned by the plugin's user.
- **Short-lived uploads:** an upload is deleted when the job that used it ends;
  an unused one after an hour; at most 200 wait at once (429).
- **Image decoding:** PNGs are decoded in pure Python (`snapshots.py`). Images
  over 16384 px on a side or 4096 × 10240 pixels in total are refused before
  inflating, and the image data is inflated only up to the size the header
  promises, so a compressed bomb can't exhaust memory. Truncated or corrupt
  PNGs are an `error` job, not a crash.
- **CPU:** at most 4 comparisons run at once; each job has a timeout (default
  120 s for SNAPSHOT) enforced by a watchdog.
- **No subprocesses**, no shell, no network calls out.

## What is NOT covered

- **No TLS.** Tokens and traffic cross the LAN in clear text when the plugin is
  bound off loopback. For remote access use a VPN or Tailscale, not a
  port-forward.
- **Plaintext secrets at rest:** the token lives in the unit/plist environment
  here and in the viewer's settings file.
- Not done: systemd sandboxing of the unit, TLS termination, rate limiting and
  connection caps (many open long-poll connections can tie up threads).

## Reporting a problem

This is a single-user home-lab project. Open an issue in this repository's issue tracker
or tell the maintainer directly; don't post working tokens in the issue.
