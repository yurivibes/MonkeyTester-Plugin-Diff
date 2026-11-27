# MonkeyTester-Plugin-Diff

The `DIFF` test plugin for MonkeyTester. It runs the **visual checks** of a
test as plugin actions:

- `DIFF: SNAPSHOT <name> [mask=…] [tolerance=…]` compares the screen with a
  baseline image kept in the test case;
- `DIFF: ASSERT_LAYOUT <expr>` checks where tagged elements sit on the screen.

The text after the action is the same grammar as the old built-in
`SNAPSHOT:` / `ASSERT_LAYOUT:` steps, which MonkeyTester no longer accepts
(they are parse errors naming the `DIFF:` replacement).

The plugin **only compares**. MonkeyTester's runner does the capturing: it
takes the screenshot, reads `/screen` (the scale) and `/elements` from the
Mobile Controller, zips the case's `snapshots/` directory, uploads the PNG and
the zip, and starts a job. The plugin keeps nothing between jobs: an upload is
deleted as soon as the job that used it ends (and after an hour if no job
uses it). The runner writes the result files (`<name>.actual.png`,
`<name>.diff.png`, `<name>.baseline.png`) into the run, as before, so the
viewer shows snapshots and the mismatch list unchanged.

MonkeyTester reaches it only through the shared **Plugin API**
(`PLUGIN_API.md` in the MonkeyTester repo, contract_version 1), including the
`source` extension: a parameter that carries `"source": "<kind>"` is filled
by the runner, never by the test author.

```
MonkeyTester runner ──capture──► Mobile Controller (screenshot, /screen, /elements)
        │
        └──HTTP (plugin API)──► diff_plugin.py :8095 (Linux or macOS)
             POST /uploads (screenshot PNG, snapshots zip)
             POST /jobs SNAPSHOT | ASSERT_LAYOUT  ──► job.result
```

## Install

| Dependency | Why | Required |
|---|---|---|
| Python 3.10 or newer | the plugin (standard library only, no packages) | yes |

```bash
git clone <repo-url> ~/Workspace/MonkeyTester-Plugin-Diff
cd ~/Workspace/MonkeyTester-Plugin-Diff
```

`~/Workspace/MonkeyTester-Plugin-Diff` is the path the systemd unit in `packaging/` expects.

### With Nix (Linux or macOS)

Needs Nix with flakes enabled (`experimental-features = nix-command flakes` in
`~/.config/nix/nix.conf`). The flake brings Python, so there is nothing else to install:

```bash
nix run .#tests            # unit tests
nix run                    # the plugin on 127.0.0.1:8095 (loopback only)
nix develop                # a shell with python3
```

### Without Nix

**Debian / Ubuntu:**

```bash
sudo apt-get install -y python3 git
```

**macOS (Homebrew):**

```bash
brew install python git
```

No virtualenv is needed: the plugin uses only the Python standard library.

### Verify

```bash
# With Nix
nix run .#tests

# Without Nix
python3 -m unittest discover -s tests
```

Then start the plugin (below) and check `curl -s localhost:8095/health`.

## Run it

```bash
nix run                     # plugin on 127.0.0.1:8095
python3 diff_plugin.py      # the same, without Nix
```

Check it with `curl -s localhost:8095/health` and `curl -s localhost:8095/actions`.

| Variable | Default | |
|---|---|---|
| `PLUGIN_PORT` | `8095` | HTTP port |
| `PLUGIN_BIND` | `127.0.0.1` | listen address; anything but loopback needs `PLUGIN_TOKEN` |
| `PLUGIN_TOKEN` | unset | the `X-Plugin-Token` every request except `GET /health` must send |
| `PLUGIN_UPLOAD_DIR` | `<tmp>/mt-diff-uploads` | where uploads wait for their job (created `0700`) |
| `PLUGIN_ACCESS_LOG` | unset | set to log every request |

### Network access and the token

The plugin API listens on **127.0.0.1 only** by default, which is all the
viewer needs when it runs on the same host (it reaches the plugin at
`http://127.0.0.1:8095`).

To reach the plugin from another machine, bind it to a LAN address **and** set a
token, or it refuses to start (exit code 2, with the reason on stderr):

```bash
export PLUGIN_TOKEN=$(openssl rand -hex 32)    # at least 16 characters
PLUGIN_BIND=0.0.0.0 PLUGIN_TOKEN=$PLUGIN_TOKEN nix run
curl -s -H "X-Plugin-Token: $PLUGIN_TOKEN" http://<plugin-host>:8095/actions
```

With a token set, every request except `GET /health` needs the
`X-Plugin-Token` header (401 otherwise); on loopback the token is optional and,
when set, enforced the same way. Enter the same token for the plugin's row on
the viewer's **Settings → Plugins** page. There is no TLS: use a VPN or
Tailscale to reach the plugin across untrusted networks. Details:
[SECURITY.md](SECURITY.md).

### Documentation page

`GET /docs` serves this README rendered as a self-contained HTML page
(`text/html`, a strict `Content-Security-Policy`, `Cache-Control: no-store`). It
needs the token like every route except `/health`. `GET /health` reports
`"docs": "/docs"` while README.md is readable next to `plugin_server.py`. The
viewer's **/plugins** page shows a **Documentation** link for it and fetches the
page itself through `/plugins/docs/DIFF`. Try it with `curl -s localhost:8095/docs`.

### Start and stop

**Linux (systemd user service):**

```bash
cp packaging/systemd/monkeytester-diff.service ~/.config/systemd/user/
#   (edit WorkingDirectory if the checkout is not ~/Workspace/MonkeyTester-Plugin-Diff)
systemctl --user daemon-reload
systemctl --user enable --now monkeytester-diff

systemctl --user restart monkeytester-diff
systemctl --user status monkeytester-diff
journalctl --user -u monkeytester-diff -f
```

**macOS (LaunchAgent):** replace `PYTHON` and `CHECKOUT_DIR` in
`packaging/launchd/com.blackjackfruit.monkeytester-diff.plist`, copy it to
`~/Library/LaunchAgents/`, and `launchctl load`/`unload` it.

### Adding it to MonkeyTester

On the viewer's **Settings → Plugins** page, add `http://127.0.0.1:8095` on the
**Test Plugins** Add row. The viewer reads `/health` and registers the plugin
under its name, `DIFF`. Without it, every `DIFF:` step fails with
`DIFF plugin is not configured: add MonkeyTester-Plugin-Diff on Settings → Plugins`.

## Test steps

```
DIFF: SNAPSHOT home mask=status_bar,nav_bar
DIFF: SNAPSHOT settings mask=status_bar,tag:clock,rect:0,500,375,40 tolerance=0.5
DIFF: ASSERT_LAYOUT title below logo gap>=8 gap<=24
DIFF: ASSERT_LAYOUT logo centered-x tol=1%
```

Baselines stay in the test case, unchanged:

```
<case>/snapshots/<platform>/<W>x<H>@<scale>x/<name>.png
```

`W x H` is the screen in points (iOS) or dp (Android), so phones with the same
screen share a baseline; images are compared at 1x (the capture is
box-averaged down from device pixels). A pixel differs when any channel moves
by more than 24; the step passes while at most `tolerance` percent (default
0.1) of the unmasked pixels differ. A missing baseline fails the step and the
message lists the screen classes that do have one. In record mode (the
viewer's record checkbox) every SNAPSHOT passes without comparing and the run
keeps the captures as candidate baselines.

Masks: `status_bar`, `nav_bar` (sized per platform and screen shape),
`tag:<testTag>` (the element's box from the controller), `rect:x,y,w,h` (pt/dp).
ASSERT_LAYOUT checks: `visible`, `inside screen|<tag>`, `centered-x`,
`centered-y`, `below|above|left-of|right-of <tag> [gap>=N] [gap<=N]`,
`no-overlap <tag>`, `aligned-left|right|top|bottom|center-x|center-y <tag>`,
with `tol=N`; numbers are pt/dp, `N%` is percent of the screen width or height.

## API

The actions, their parameters (with `source`), the upload rules and the exact
`job.result` shapes are in [ACTIONS.md](ACTIONS.md). In short:

- `POST /uploads` takes the raw PNG (`Content-Type: image/png`) or a zip of the
  case's `snapshots/` directory (`Content-Type: application/zip`) and answers
  `201 {"upload": "upload:<id>", "path": "<host path>"}`. Upload once per job:
  the plugin deletes the upload when the job ends.
- `POST /jobs {"action": "SNAPSHOT", "params": {…}}`, then
  `GET /jobs/<id>?wait=30` until the job is terminal. `passed` for
  match/recorded, `failed` for mismatch/missing, `error` for bad arguments or a
  bad image; `job.result` carries the outcome in every case.

## Layout

| File | What |
|---|---|
| `diff_plugin.py` | The DIFF plugin: the SNAPSHOT and ASSERT_LAYOUT actions, input checks, `main()` |
| `snapshots.py` | PNG codec, screen classes, masks and the image compare (ported from MonkeyTester) |
| `layout.py` | The ASSERT_LAYOUT checks (ported verbatim from MonkeyTester) |
| `plugin_server.py` | The generic plugin API server: jobs, long-poll waits, uploads, `/docs`. No image code. |
| `security.py` | Bind/token policy and validators (job/upload ids, baseline zip members). See [SECURITY.md](SECURITY.md). |
| `docs_page.py` | Renders README.md into the `GET /docs` HTML page. Shared verbatim by every MonkeyTester plugin; don't edit the copy here. |
| `tests/` | Compare tests (ported from MonkeyTester), HTTP contract and action tests, security tests |
