# DIFF plugin actions

Every action runs as a job (`POST /jobs`, then `GET /jobs/<id>?wait=…`; see
`PLUGIN_API.md` in MonkeyTester). `GET /actions` returns the same list with full
parameter schemas. Both actions are `needs_device: false`: the plugin has no
devices (`GET /devices` is an empty list), so a job carries no `device` (a
non-empty `device` is a `404 unknown_device`).

Action names are uppercase, `SNAPSHOT` and `ASSERT_LAYOUT`. `POST /jobs` also
accepts them in any case (`snapshot`) and reports the canonical name in the
job's `action`.

## Runner-supplied params (`source`)

Every parameter of both actions carries `"source": "<kind>"`: MonkeyTester's
runner fills it itself before it starts the job, and a test author never writes
it. The test step is just `DIFF: SNAPSHOT <args>` / `DIFF: ASSERT_LAYOUT <args>`.

| `source` | Param type | What the runner sends |
|---|---|---|
| `args` | `string` | the raw rest of the step line after the action (`home mask=status_bar tolerance=0.5`, `title below logo gap>=8`), `${VAR}`s substituted; not split into `key=value` params |
| `screenshot` | `path` | a controller screenshot (device px PNG) uploaded raw with `POST /uploads` (`Content-Type: image/png`), passed as `upload:<id>` |
| `screen` | `json` | the controller's `/screen` object `{width, height, scale, …}` (device px) |
| `elements` | `json` | the controller's `/devices/<id>/elements` list `[{id, x, y, w, h}, …]` (device px); a `{"elements": [...]}` envelope is accepted too |
| `platform` | `enum` | `android` or `ios` (`DEVICE_PLATFORM`) |
| `baselines` | `path` | the current case's `snapshots/` directory zipped (members `snapshots/<platform>/<W>x<H>@<scale>x/<name>.png`; an empty zip when the case has none) and uploaded (`Content-Type: application/zip`), passed as `upload:<id>` |
| `record` | `bool` | MonkeyTester's record mode (`snapshots.record_mode()`) |

`SNAPSHOT`'s `elements` is optional and marked `"when": "tag_mask"`: it is only
needed when `args` contains a `tag:` mask. A runner may always send it; without
it a `tag:` mask ends the job in `error` (`a tag: mask needs the elements list`).

`path` params accept **only** `upload:<id>` refs: the plugin may run on another
host and reads no host paths from requests (a host path is `400 bad_request`).

## `GET /actions`

```json
{
  "actions": [
    {
      "name": "SNAPSHOT",
      "description": "Compare the screen with the case's baseline (snapshots/<platform>/<W>x<H>@<scale>x/<name>.png) at 1x. Test step: DIFF: SNAPSHOT <name> [mask=...] [tolerance=...]. passed: match or recorded; failed: mismatch or missing baseline.",
      "params": {
        "args": {"type": "string", "required": true, "description": "…", "source": "args"},
        "screenshot": {"type": "path", "required": true, "description": "…", "source": "screenshot"},
        "screen": {"type": "json", "required": true, "description": "…", "source": "screen"},
        "platform": {"type": "enum", "required": true, "description": "…", "values": ["android", "ios"], "source": "platform"},
        "baselines": {"type": "path", "required": true, "description": "…", "source": "baselines"},
        "elements": {"type": "json", "required": false, "description": "…", "source": "elements", "when": "tag_mask"},
        "record": {"type": "bool", "required": false, "description": "…", "default": false, "source": "record"}
      },
      "default_timeout_ms": 120000,
      "needs_device": false
    },
    {
      "name": "ASSERT_LAYOUT",
      "description": "Check where tagged elements sit, in pt/dp. Test step: DIFF: ASSERT_LAYOUT <tag> <check> [<tag2>] [tol=N] [gap>=N] [gap<=N].",
      "params": {
        "args": {"type": "string", "required": true, "description": "…", "source": "args"},
        "screen": {"type": "json", "required": true, "description": "…", "source": "screen"},
        "elements": {"type": "json", "required": true, "description": "…", "source": "elements"}
      },
      "default_timeout_ms": 30000,
      "needs_device": false
    }
  ]
}
```

## `SNAPSHOT`

`args`: `<name> [mask=<m>[,<m>…]] [tolerance=<percent>]`, exactly the grammar
of the old `SNAPSHOT:` step. `<name>` uses letters, digits, `_ . -`. Masks:
`status_bar`, `nav_bar`, `tag:<testTag>`, `rect:x,y,w,h` (pt/dp). `tolerance`
is 0–100, default `0.1`.

What the plugin does: decode the screenshot, box-average it to 1x
(`round(px / scale)`), work out the screen class
`<platform>/<W>x<H>@<scale>x`, and look for `snapshots/<class>/<name>.png` in
the baselines upload. A pixel differs when any channel moves by more than 24;
the snapshot matches while at most `tolerance` % of the unmasked pixels differ.

`job.result` always has these keys (null where they don't apply):

```json
{
  "name": "home",
  "status": "match",
  "message": "0.000% of pixels differ (tolerance 0.1%) against ios/375x667@2x/home.png",
  "screen_class": "ios/375x667@2x",
  "diff_percent": 0.0,
  "tolerance": 0.1,
  "baseline_path": "ios/375x667@2x/home.png",
  "available_classes": ["ios/375x667@2x"],
  "actual_png_b64": "<base64 PNG, the 1x capture>",
  "diff_png_b64": "<base64 PNG, or null>"
}
```

| `status` | Job state | `job.error.code` | `diff_percent` | `diff_png_b64` |
|---|---|---|---|---|
| `match` | `passed` | — (`error: null`) | number | PNG |
| `recorded` (record mode; not compared) | `passed` | — | null | null |
| `mismatch` (pixels) | `failed` | `mismatch` | number | PNG |
| `mismatch` (baseline has another size: `baseline is WxH, capture is WxH at 1x`) | `failed` | `mismatch` | null | null |
| `missing` | `failed` | `missing` | null | null |
| `error` (bad args, bad PNG, bad screen/elements, tag not on screen, wrong upload kind) | `error` | `bad_input` | null | null |

- `baseline_path` is relative to the case's `snapshots/` directory
  (`<class>/<name>.png`), the same value MonkeyTester has always written to
  `step.snapshot.baseline_path`. It is set for every status but `error`.
- `available_classes`: the screen classes in the upload that have a
  `<name>.png`, sorted. A `missing` message reads
  `baseline missing: snapshots/<class>/<name>.png (baselines exist for: a, b)`
  or `… (no baselines for this snapshot yet)`.
- `actual_png_b64` is the 1x capture (what the runner saves as
  `<name>.actual.png`, and what becomes the baseline after recording). Present
  for every status but `error`.
- The plugin never returns the baseline: the runner already has it in the case
  (`snapshots/<baseline_path>`) and copies it to `<name>.baseline.png` for
  `match` and `mismatch`.
- On `error`, `result` is `{"name": <name or null>, "status": "error",
  "message": <reason>, …all other keys null, "available_classes": []}`.

## `ASSERT_LAYOUT`

`args`: `<tag> <check> [<tag2>] [options]`, exactly the grammar of the old
`ASSERT_LAYOUT:` step (see `layout.py`): `visible`, `inside screen|<tag>`,
`centered-x`, `centered-y`, `below|above|left-of|right-of <tag>`,
`no-overlap <tag>`, `aligned-left|right|top|bottom|center-x|center-y <tag>`;
options `tol=N`, `gap>=N`, `gap<=N` (pt/dp, or `N%` of the screen).

`job.result` is `{"passed": <bool>, "message": "<text>"}`:

| Outcome | Job state | `job.error.code` |
|---|---|---|
| check holds | `passed` | — |
| check fails (also: tag not on screen, tag not unique) | `failed` | `layout` |
| bad syntax, bad `screen` / `elements` | `error` | `bad_input` (`result.passed` is false) |

## Uploads

`POST /uploads` (capability `uploads`) takes the raw body, with a
`Content-Length` (chunked bodies are 411), up to 64 MiB. The kind is read from
the bytes; send the matching `Content-Type` anyway:

| Body | Stored as | Reply `path` |
|---|---|---|
| a PNG (`image/png`) | `<upload dir>/upload.png` | that file |
| a zip (`application/zip`) | unpacked under `<upload dir>/files/` | that directory |

Reply: `201 {"upload": "upload:<12 hex>", "path": "<host path>"}`. Anything
else is `400 bad_request`.

A zip must hold only `snapshots/<android|ios>/<W>x<H>@<scale>x/<name>.png`
files — the same pattern as MonkeyTester's `SNAPSHOT_PATH_RE` — plus, if the
zip tool adds them, the directory entries leading to them. Any other entry
(`__MACOSX/`, a `.txt`, a subfolder, `..`, an absolute path, a backslash, a
symlink) refuses the **whole** upload with 400. Each file must be a non-empty
PNG of at most 8 MiB; at most 1000 files and 256 MiB unpacked (413 above).
An empty zip is fine: no baselines.

Uploads are **single-use**: the plugin deletes an upload when the job that used
it ends (in any state). Upload the screenshot and the baselines again for every
job; a used ref is `400` (`doesn't exist (expired, or already used by a job:
upload once per job)`). An upload no job uses is deleted after an hour, and at
most 200 may wait at once (429 `too_many_uploads`).

## Timeouts and cancelling

`SNAPSHOT` defaults to 120 s, `ASSERT_LAYOUT` to 30 s (`timeout_ms` may set
another, at most one hour). At most 4 jobs compare at once; more wait in
`queued`. `POST /jobs/<id>/cancel` ends a job `cancelled`.
