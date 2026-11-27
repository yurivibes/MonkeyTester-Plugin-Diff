# MonkeyTester-Plugin-Diff — Agent Guide

## Purpose
The `DIFF` test plugin for MonkeyTester. It runs a test's visual checks as
plugin actions: `DIFF: SNAPSHOT <name> [mask=…] [tolerance=…]` compares the
screen with a baseline image kept in the test case, and
`DIFF: ASSERT_LAYOUT <expr>` checks where tagged elements sit. The plugin only
**compares**: MonkeyTester's runner captures the screenshot, `/screen` and
`/elements` from the Mobile Controller and uploads them with the case's
`snapshots/` zip. See README.md and ACTIONS.md.

## Setup (do this first)
Install the dependencies with the steps in README.md → **Install** before running or testing
anything; that section is the single source for them. In short:
- **Nix available** (`command -v nix`): nothing to install. `nix run .#tests` runs the tests,
  `nix run` starts the plugin on 127.0.0.1:8095, `nix develop` gives a shell with Python.
- **No Nix:** Python 3.10+. Standard library only: no packages, no `pip install`.
- Don't try a change on port 8095 while the live `monkeytester-diff` service runs; start the
  plugin on another port (`PLUGIN_PORT=18095 python3 diff_plugin.py`).
- A new dependency goes into README.md → Install and `flake.nix` in the same PR. Keep the
  plugin stdlib-only unless the user agrees otherwise. If a step in README.md → Install doesn't
  work as written, fix it in the same PR.

## Critical Rules
- **The plugin API is a contract.** `plugin_server.py` implements `PLUGIN_API.md` from the
  MonkeyTester repo (contract_version 1), including its **runner-supplied params**: a param
  spec carrying `"source": "<kind>"` (`args`, `screenshot`, `screen`, `elements`, `platform`,
  `baselines`, `record`) is filled by the runner, never by the test author. Don't add or change
  endpoints, job states, `source` kinds or result fields here without changing that document
  first (and bumping `contract_version` for breaking changes).
- **Stateless.** The plugin keeps nothing between jobs. An upload is single-use: it is deleted
  when the job that used it ends (any state) and after 1 h if no job uses it. Don't add
  baseline storage, caches or artifact endpoints; the runner writes `<name>.actual/.diff/
  .baseline.png` into the run from `job.result`.
- **Result shapes are what the runner and viewer read.** SNAPSHOT `job.result` always carries
  every key (`name, status, message, screen_class, diff_percent, tolerance, baseline_path,
  available_classes, actual_png_b64, diff_png_b64`; null where unused). ASSERT_LAYOUT returns
  exactly `{passed, message}`. MonkeyTester detects the shape by these keys, so renaming or
  adding keys there is a contract change.
- **Compare rules are user-decided** (MonkeyTester `SNAPSHOTS.md`): baselines are stored at 1x
  per screen class `<platform>/<W>x<H>@<scale>x` (scale rounded to 3 decimals, `%g`); never
  reuse or rescale a baseline across classes; a pixel differs when any channel moves by more
  than 24; default tolerance 0.1% of unmasked pixels; a missing baseline FAILS and lists the
  classes that have one; record mode returns `recorded` and passes. `snapshots.py` and
  `layout.py` are the only copies now (MonkeyTester removed its own), so grammar changes
  happen here and in MonkeyTester's `SNAPSHOTS.md` together.
- **Untrusted input.** Uploaded zips must contain only
  `snapshots/(android|ios)/<W>x<H>@<scale>x/<name>.png` members (same rule as MonkeyTester's
  `SNAPSHOT_PATH_RE`); anything else, `..` or absolute paths reject the whole zip. Keep the
  size caps in `plugin_server.py` (`MAX_*`) and the checks in `diff_plugin.py`
  (`check_screen`, `check_elements`, `_check_args`). Path params accept only `upload:<id>`.
- **Network rule (family-wide):** default bind `127.0.0.1`; on any non-loopback bind the plugin
  refuses to start without `PLUGIN_TOKEN` (`X-Plugin-Token`, compared with
  `hmac.compare_digest`). `/health` stays open and leaks nothing. See SECURITY.md.
- **No device code.** The plugin never calls the Mobile Controller, adb or Appium; capturing is
  the runner's job (`needs_device: false`, a `device` value is refused).
- **Must run on Linux and macOS.**
- Keep ACTIONS.md in step with the actions built in `diff_plugin.py`.
- **`GET /docs` renders README.md** through `docs_page.py`, a renderer copied verbatim into every
  MonkeyTester plugin: don't edit the copy here (report a renderer bug instead). `/health`
  reports `"docs": "/docs"` only while README.md is readable; keep README.md accurate, it is the
  page users read from /plugins.
- **MOTION** (transition check on the test video) is planned for this plugin in a follow-up PR;
  MonkeyTester keeps the name reserved. Agree its design with the user before coding.
- **Keep the user informed of user-relevant changes.** A change is user-relevant when it changes
  what a person running the plugin does or sees: install, start, stop or restart (systemd unit,
  LaunchAgent, `nix run`), the port (8095), environment variables (`PLUGIN_PORT`, `PLUGIN_BIND`,
  `PLUGIN_TOKEN`, `PLUGIN_UPLOAD_DIR`, `PLUGIN_ACCESS_LOG`), the curl/HTTP commands in README.md,
  or test step syntax (`DIFF: …`). For every such change:
  - update README.md (and ACTIONS.md for actions) **in the same PR**; the README commands must
    work as written;
  - add a **"For the user"** section to the PR description saying in plain words what changed
    for them and what, if anything, they must do;
  - call it out in the report to the user, not only in the PR.
  If a change is not user-relevant, say "For the user: no change" in the PR.

## Tests
`nix run .#tests` (or `python3 -m unittest discover -s tests`). `tests/test_snapshots.py`
covers the PNG decode/encode, masks and compare; `tests/test_plugin.py` drives the HTTP server
on a random 127.0.0.1 port (uploads, jobs, every SNAPSHOT status, ASSERT_LAYOUT, bad zips);
`tests/test_security.py` the bind/token policy; `tests/test_docs_page.py` the `/docs` page.
Tests need no device and no MonkeyTester checkout. When a change touches the result shape or a
`source` param, also run MonkeyTester's `tests/test_diff_plugin_steps.py` or one runner job
against this plugin on a scratch port.

## Hosting and review workflow
This repo isn't tied to one code host: it can live on GitHub, Gitea/Forgejo, GitLab, Bitbucket or a
mirror. Find the host from the remote you were asked to work against (`git remote -v`, usually
`origin`) and use that host's tools; the docs never assume one.

| Remote host | Review is called | CLI |
|---|---|---|
| github.com or GitHub Enterprise | pull request | `gh pr create` |
| Gitea / Forgejo | pull request | `tea pr create` |
| GitLab | merge request | `glab mr create` |
| Bitbucket | pull request | web UI or REST API |

- The default branch (`main` on the GitHub repo) changes only through a reviewed PR/MR that a
  maintainer merges. Never push to it and never merge your own PR/MR.
- Work on a feature branch in its own git worktree, based on the remote's default branch.
- There is no hosted CI. Run `nix run .#tests` (or the `python3 -m unittest` line in Tests)
  before opening the PR/MR.
- In docs, name sibling repos (e.g. MonkeyTester-Plugin-AI) instead of linking a host URL, link
  files in this repo with relative paths, and stick to CommonMark plus GFM tables and code
  fences (no host-only syntax such as `> [!NOTE]` callouts or `#123` autolinks).
