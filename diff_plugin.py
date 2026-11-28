"""MonkeyTester DIFF plugin: SNAPSHOT and ASSERT_LAYOUT as plugin actions.

The MonkeyTester runner captures (controller screenshot, `/screen`,
`/elements`), uploads the screenshot and the case's `snapshots/` directory, and
starts a job; this plugin only compares and keeps nothing between jobs (its
uploads are deleted when the job that used them ends). See README.md and
ACTIONS.md.

Environment:
  PLUGIN_PORT        — HTTP port (default 8095)
  PLUGIN_BIND        — listen address (default 127.0.0.1; anything else needs PLUGIN_TOKEN)
  PLUGIN_TOKEN       — X-Plugin-Token every request but GET /health must send
  PLUGIN_UPLOAD_DIR  — where uploads wait for their job (default <tmp>/mt-diff-uploads)
  PLUGIN_ACCESS_LOG  — set to log every request

Standard library only.
"""

from __future__ import annotations

import base64
import math
import os
import signal
import sys

import layout
import plugin_server as ps
import security
import snapshots
from plugin_server import Action, ActionError, ActionFailed, Param, PluginServer

PLUGIN_NAME = "DIFF"
VERSION = "0.1.0"
PLUGIN_DEFAULT_PORT = 8095

MAX_ELEMENTS = 10000
MAX_ARGS_LEN = 4096
MAX_SCALE = 10.0

SOURCE_DOC = "Filled by MonkeyTester (source param); tests never write it."


def _b64(data):
    return base64.b64encode(data).decode("ascii") if data is not None else None


def snapshot_result(name=None, status="error", message="", **fields):
    """A SNAPSHOT job.result: always every key, null where unknown."""
    out = {"name": name, "status": status, "message": message, "screen_class": None,
           "diff_percent": None, "tolerance": None, "baseline_path": None,
           "available_classes": [], "actual_png_b64": None, "diff_png_b64": None}
    out.update(fields)
    return out


def _number(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} must be a number")
    return value


def check_screen(screen):
    """The controller's /screen dict: width, height (device px) and scale > 0."""
    if not isinstance(screen, dict):
        raise ValueError("screen must be the controller's /screen object {width, height, scale}")
    for key in ("width", "height", "scale"):
        if key not in screen:
            raise ValueError(f"screen has no {key!r} (an old Mobile Controller without "
                             "a screen scale?)")
        if _number(screen[key], f"screen.{key}") <= 0:
            raise ValueError(f"screen.{key} must be more than 0")
    if screen["scale"] > MAX_SCALE:
        raise ValueError(f"screen.scale must be at most {MAX_SCALE:g}")
    return screen


def check_elements(elements):
    """The controller's /elements list [{id, x, y, w, h}] (device px)."""
    if isinstance(elements, dict) and isinstance(elements.get("elements"), list):
        elements = elements["elements"]  # the controller's envelope, accepted too
    if not isinstance(elements, list):
        raise ValueError("elements must be a list of {id, x, y, w, h}")
    if len(elements) > MAX_ELEMENTS:
        raise ValueError(f"more than {MAX_ELEMENTS} elements")
    out = []
    for i, e in enumerate(elements):
        if not isinstance(e, dict):
            raise ValueError(f"elements[{i}] is not an object")
        for key in ("x", "y", "w", "h"):
            _number(e.get(key), f"elements[{i}].{key}")
        if e.get("id") is not None and not isinstance(e.get("id"), str):
            raise ValueError(f"elements[{i}].id must be a string")
        out.append(e)
    return out


def _check_args(args):
    if not isinstance(args, str) or len(args) > MAX_ARGS_LEN or "\0" in args:
        raise ValueError(f"args must be a string of at most {MAX_ARGS_LEN} characters")
    return args


class DiffPlugin(ps.Plugin):
    name = PLUGIN_NAME
    default_name = PLUGIN_NAME
    version = VERSION
    capabilities = ["uploads"]

    def __init__(self, upload_dir: str | None = None):
        super().__init__(upload_dir or os.environ.get("PLUGIN_UPLOAD_DIR") or None)
        self._actions = self._build_actions()

    def actions(self) -> dict:
        return self._actions

    # ── actions ──

    def _build_actions(self) -> dict:
        snapshot = Action(
            name="SNAPSHOT",
            description=("Compare the screen with the case's baseline "
                         "(snapshots/<platform>/<W>x<H>@<scale>x/<name>.png) at 1x. "
                         "Test step: DIFF: SNAPSHOT <name> [mask=...] [tolerance=...]. "
                         "passed: match or recorded; failed: mismatch or missing baseline."),
            run=self._snapshot,
            params={
                "args": Param("string", "The step text after the action: "
                              "<name> [mask=status_bar,nav_bar,tag:<id>,rect:x,y,w,h] "
                              "[tolerance=<percent>]. " + SOURCE_DOC,
                              required=True, source="args"),
                "screenshot": Param("path", "The controller screenshot (device px PNG), "
                                    "uploaded raw (image/png): upload:<id>. " + SOURCE_DOC,
                                    required=True, source="screenshot"),
                "screen": Param("json", "The controller's /screen {width, height, scale}. "
                                + SOURCE_DOC, required=True, source="screen"),
                "platform": Param("enum", "DEVICE_PLATFORM. " + SOURCE_DOC, required=True,
                                  values=["android", "ios"], source="platform"),
                "baselines": Param("path", "The case's snapshots/ directory as a zip "
                                   "(members snapshots/<android|ios>/<W>x<H>@<scale>x/"
                                   "<name>.png; may be empty): upload:<id>. " + SOURCE_DOC,
                                   required=True, source="baselines"),
                "elements": Param("json", "The controller's /elements [{id,x,y,w,h}] (device "
                                  "px). Needed only when args has a tag: mask. " + SOURCE_DOC,
                                  source="elements", when="tag_mask"),
                "record": Param("bool", "Record mode: return the capture and pass without "
                                "comparing. " + SOURCE_DOC, default=False, source="record"),
            },
            default_timeout_ms=120_000,
        )
        assert_layout = Action(
            name="ASSERT_LAYOUT",
            description=("Check where tagged elements sit, in pt/dp. Test step: "
                         "DIFF: ASSERT_LAYOUT <tag> <check> [<tag2>] [tol=N] [gap>=N] [gap<=N]."),
            run=self._assert_layout,
            params={
                "args": Param("string", "The step text after the action: the layout "
                              "expression, e.g. `title below logo gap>=8`. " + SOURCE_DOC,
                              required=True, source="args"),
                "screen": Param("json", "The controller's /screen {width, height, scale}. "
                                + SOURCE_DOC, required=True, source="screen"),
                "elements": Param("json", "The controller's /elements [{id,x,y,w,h}] "
                                  "(device px). " + SOURCE_DOC, required=True,
                                  source="elements"),
            },
            default_timeout_ms=30_000,
        )
        return {a.name: a for a in (snapshot, assert_layout)}

    def _snapshot(self, ctx, params):
        name = None
        try:
            args = _check_args(params["args"])
            name, masks, tolerance = snapshots.parse_args(args)
            screen = check_screen(params["screen"])
            elements = params.get("elements")
            if elements is not None:
                elements = check_elements(elements)
            elif snapshots.needs_elements(masks):
                raise snapshots.SnapshotError(
                    "a tag: mask needs the elements list (param 'elements', source elements)")
            capture = self._upload_png(params["screenshot"])
            root = self._baseline_root(params["baselines"])
            ctx.log(f"SNAPSHOT {name}: capture {'x'.join(map(str, snapshots.png_size(capture)))}"
                    f" px, scale {screen['scale']:g}, platform {params['platform']}"
                    + (", record mode" if params.get("record") else ""))
            r = snapshots.take(name, capture_png=capture, platform=params["platform"],
                               scale=float(screen["scale"]), snapshot_root=root, masks=masks,
                               tolerance=tolerance, elements=elements,
                               record=bool(params.get("record")), check=ctx.check)
        except (snapshots.SnapshotError, ValueError) as e:
            raise ActionError(str(e), "bad_input",
                              result=snapshot_result(name, "error", str(e))) from None
        result = snapshot_result(
            r["name"], r["status"], r["message"], screen_class=r["screen_class"],
            diff_percent=r["diff_percent"], tolerance=r["tolerance"],
            baseline_path=r["baseline_path"], available_classes=r["available_classes"],
            actual_png_b64=_b64(r["actual_png"]), diff_png_b64=_b64(r["diff_png"]))
        line = f"SNAPSHOT {r['name']} [{r['screen_class']}]: {r['status'].upper()} - {r['message']}"
        if r["status"] in ("match", "recorded"):
            ctx.log(line)
            return result
        raise ActionFailed(line, r["status"], result=result)

    def _assert_layout(self, ctx, params):
        try:
            args = _check_args(params["args"])
            layout.parse(args)
            screen = check_screen(params["screen"])
            elements = check_elements(params["elements"])
            passed, message = layout.check(args, screen, elements)
        except (layout.LayoutError, ValueError) as e:
            raise ActionError(str(e), "bad_input",
                              result={"passed": False, "message": str(e)}) from None
        line = f"LAYOUT {'OK' if passed else 'FAILED'}: {message}"
        if passed:
            ctx.log(line)
            return {"passed": True, "message": message}
        raise ActionFailed(line, "layout", result={"passed": False, "message": message})

    # ── uploads ──

    def _upload_png(self, upload_dir):
        path = os.path.join(upload_dir, ps.UPLOAD_PNG_NAME)
        if not os.path.isfile(path) or os.path.islink(path):
            raise ValueError("screenshot must be a PNG upload (POST /uploads with the PNG "
                             "bytes, Content-Type: image/png)")
        with open(path, "rb") as f:
            return f.read()

    def _baseline_root(self, upload_dir):
        files = os.path.join(upload_dir, ps.UPLOAD_ZIP_DIR)
        if not os.path.isdir(files) or os.path.islink(files):
            raise ValueError("baselines must be a zip upload of the case's snapshots/ "
                             "directory (Content-Type: application/zip)")
        root = os.path.join(files, "snapshots")
        if not security.within(root, files):
            raise ValueError("bad baselines upload")
        return root  # may not exist: an empty zip means no baselines at all


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def main() -> None:
    # Bind/token policy first: a non-loopback bind without a (long enough)
    # PLUGIN_TOKEN refuses to start (SECURITY.md).
    bind, token = security.bind_and_token_from_env()
    err = security.bind_policy_error(bind, token)
    if err:
        print(f"{PLUGIN_NAME}: {err}", file=sys.stderr, flush=True)
        sys.exit(2)
    plugin = DiffPlugin()
    server = PluginServer(plugin, bind, _env_int("PLUGIN_PORT", PLUGIN_DEFAULT_PORT), token)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"MonkeyTester {plugin.name} {VERSION} (contract {ps.CONTRACT} "
          f"v{ps.CONTRACT_VERSION}) on {bind}:{server.port} "
          f"(token {'required' if token else 'off'}); uploads in {plugin.uploads.root}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
