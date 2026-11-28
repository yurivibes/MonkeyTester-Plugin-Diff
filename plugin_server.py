"""Generic MonkeyTester plugin API server (contract_version 1).

Implements the HTTP side of PLUGIN_API.md (in the MonkeyTester repo): health,
devices, actions, jobs (start / long-poll wait / cancel), uploads and the
docs page. Adapted from MonkeyTester-Plugin-Server's plugin_server.py for a
plugin without hardware: no events, every action is `needs_device: false`,
uploads take a PNG or a baselines zip and are deleted once a job used them.
It knows nothing about images: a plugin hands it a `Plugin` object (see
diff_plugin.py) and gets the whole contract for free.

Standard library only.
"""

from __future__ import annotations

import hmac
import io
import json
import os
import platform
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import security
from docs_page import docs_html

CONTRACT = "monkeytester-plugin"
CONTRACT_VERSION = 1

TERMINAL_STATES = {"passed", "failed", "timeout", "error", "cancelled"}
JOB_RETENTION_S = 15 * 60
MAX_RETAINED_JOBS = 500                   # finished jobs kept (oldest dropped first)
JOB_LOG_LINES = 500
MAX_WAIT_S = 60
MAX_CONCURRENT_JOBS = 4                   # comparisons are CPU-bound pure Python
MAX_TIMEOUT_MS = 60 * 60 * 1000
HANDLER_SOCKET_TIMEOUT_S = 30
MAX_JSON_BYTES = 8 * 1024 * 1024          # every non-upload body (elements lists can be long)

UPLOAD_TTL_S = 60 * 60                    # an unused upload is deleted after an hour
MAX_UPLOAD_BYTES = 64 * 1024 * 1024       # POST /uploads body (a PNG or a zip)
MAX_LIVE_UPLOADS = 200                    # uploads on disk at once
MAX_ZIP_FILES = 1000                      # baselines per zip (MonkeyTester: MAX_SNAPSHOT_FILES)
MAX_ZIP_ENTRIES = 4000                    # files + directory entries
MAX_ZIP_MEMBER_BYTES = 8 * 1024 * 1024    # one baseline (MonkeyTester: MAX_SNAPSHOT_BYTES)
MAX_UNZIPPED_BYTES = 256 * 1024 * 1024    # all baselines of one upload, unpacked
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
UPLOAD_PNG_NAME = "upload.png"            # a PNG upload: <upload dir>/upload.png
UPLOAD_ZIP_DIR = "files"                  # a zip upload: unpacked under <upload dir>/files/

# GET /docs renders this file at request time (resolved next to this module, not
# the CWD). /health advertises "docs" only while it is readable.
README_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")
DOCS_PATH = "/docs"
DOCS_CSP = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")


def readme_available() -> bool:
    return os.path.isfile(README_PATH) and os.access(README_PATH, os.R_OK)


def now_ms() -> int:
    return int(time.time() * 1000)


# ───────────────────────────── action outcomes ─────────────────────────────


class ActionFailed(Exception):
    """The action ran but the result was wrong (a test failure) -> state `failed`."""

    def __init__(self, message: str, code: str = "failed", result=None):
        super().__init__(message)
        self.code = code
        self.result = result


class ActionError(Exception):
    """The plugin couldn't run the action (bad input) -> state `error`."""

    def __init__(self, message: str, code: str = "plugin_error", result=None):
        super().__init__(message)
        self.code = code
        self.result = result


class JobTimeout(Exception):
    """Raised inside an action when its deadline passes -> state `timeout`."""


class JobCancelled(Exception):
    """Raised inside an action after POST /jobs/<id>/cancel -> state `cancelled`."""


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


# ───────────────────────────── action schema ─────────────────────────────


@dataclass
class Param:
    type: str  # string | int | float | bool | enum | path | json
    description: str = ""
    required: bool = False
    default: object = None
    values: list | None = None  # for enum
    # Runner-supplied param (PLUGIN_API.md `source`): MonkeyTester fills it
    # itself before starting the job; the test author never writes it.
    source: str | None = None
    # Optional hint for a source param: when the runner needs to fill it.
    when: str | None = None

    def to_json(self):
        out = {"type": self.type, "required": self.required}
        if self.description:
            out["description"] = self.description
        if self.default is not None:
            out["default"] = self.default
        if self.values is not None:
            out["values"] = list(self.values)
        if self.source is not None:
            out["source"] = self.source
        if self.when is not None:
            out["when"] = self.when
        return out


@dataclass
class Action:
    name: str
    description: str
    run: object  # callable(ctx: JobContext, params: dict) -> dict | None
    params: dict = field(default_factory=dict)  # name -> Param
    default_timeout_ms: int = 30_000
    needs_device: bool = False

    def to_json(self):
        return {
            "name": self.name,
            "description": self.description,
            "params": {k: p.to_json() for k, p in self.params.items()},
            "default_timeout_ms": self.default_timeout_ms,
            "needs_device": self.needs_device,
        }


def _coerce(name: str, p: Param, value, resolve_path):
    """Validate/coerce one param value. Strings are accepted for every type so
    test steps (`key=value`) work without the client knowing the schema."""
    t = p.type
    try:
        if t == "string":
            if not isinstance(value, str):
                raise ValueError("expected a string")
            return value
        if t == "int":
            if isinstance(value, bool):
                raise ValueError("expected an integer")
            if isinstance(value, str):
                value = int(value.strip(), 0)
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if not isinstance(value, int):
                raise ValueError("expected an integer")
            return value
        if t == "float":
            if isinstance(value, bool):
                raise ValueError("expected a number")
            if isinstance(value, str):
                value = float(value.strip())
            if not isinstance(value, (int, float)):
                raise ValueError("expected a number")
            return float(value)
        if t == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"):
                return True
            if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off"):
                return False
            raise ValueError("expected a boolean")
        if t == "enum":
            if value not in (p.values or []):
                raise ValueError(f"expected one of {p.values}")
            return value
        if t == "path":
            if not isinstance(value, str) or not value:
                raise ValueError("expected a path")
            return resolve_path(value)
        if t == "json":
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return value
            return value
    except HttpError:
        raise
    except (ValueError, TypeError) as e:
        raise HttpError(400, "bad_request", f"param {name!r}: {e}") from None
    raise HttpError(500, "internal", f"param {name!r} has unknown type {t!r}")


def validate_params(action: Action, params: dict, resolve_path) -> dict:
    if not isinstance(params, dict):
        raise HttpError(400, "bad_request", "params must be an object")
    unknown = sorted(set(params) - set(action.params))
    if unknown:
        raise HttpError(400, "bad_request",
                        f"unknown param(s) for {action.name}: {', '.join(unknown)}")
    out = {}
    for name, p in action.params.items():
        if name in params and params[name] is not None:
            out[name] = _coerce(name, p, params[name], resolve_path)
        elif p.required:
            raise HttpError(400, "bad_request", f"missing required param {name!r}")
        else:
            out[name] = p.default
    return out


# ───────────────────────────── jobs ─────────────────────────────


class JobContext:
    """Handed to an action. Gives it logging, the deadline and cancellation."""

    def __init__(self, job: "Job", manager: "JobManager"):
        self._job = job
        self._manager = manager
        self.deadline = time.monotonic() + job.timeout_ms / 1000.0
        self.cancel_event = threading.Event()
        self.started_monotonic = time.monotonic()

    @property
    def job_id(self) -> str:
        return self._job.id

    def log(self, line: str) -> None:
        self._manager.append_log(self._job, line)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def check(self) -> None:
        """Raise if the job was cancelled or ran out of time."""
        if self.cancel_event.is_set():
            raise JobCancelled()
        if self.remaining() <= 0:
            raise JobTimeout()


@dataclass
class Job:
    id: str
    device: str | None
    action: str
    params: dict
    timeout_ms: int
    state: str = "queued"
    started_ms: int | None = None
    finished_ms: int | None = None
    result: object = None
    error: dict | None = None
    log: deque = field(default_factory=lambda: deque(maxlen=JOB_LOG_LINES))
    ctx: JobContext | None = None
    created_monotonic: float = field(default_factory=time.monotonic)
    # Upload dirs this job's path params resolved to; deleted when it ends.
    uploads: list = field(default_factory=list)
    # The validated params handed to the action (path params as host paths).
    resolved: dict = field(default_factory=dict)

    def to_json(self):
        return {
            "id": self.id,
            "device": self.device,
            "action": self.action,
            "params": self.params,
            "state": self.state,
            "started_ms": self.started_ms,
            "finished_ms": self.finished_ms,
            "result": self.result,
            "error": self.error,
            "log": list(self.log),
        }


class JobManager:
    def __init__(self, plugin):
        self.plugin = plugin
        self._jobs: dict[str, Job] = {}
        self._cond = threading.Condition()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)

    # -- bookkeeping ----------------------------------------------------
    def append_log(self, job: Job, line: str) -> None:
        with self._cond:
            job.log.append(line)

    def _set_state(self, job: Job, state: str, **changes) -> None:
        with self._cond:
            if job.state in TERMINAL_STATES:
                return
            job.state = state
            for k, v in changes.items():
                setattr(job, k, v)
            if state in TERMINAL_STATES:
                job.finished_ms = now_ms()
            self._cond.notify_all()

    def _prune(self) -> None:
        cutoff = time.monotonic() - JOB_RETENTION_S
        done = sorted((j for j in self._jobs.values() if j.state in TERMINAL_STATES),
                      key=lambda j: j.created_monotonic)
        excess = len(done) - MAX_RETAINED_JOBS
        for i, j in enumerate(done):
            if j.created_monotonic < cutoff or i < excess:
                del self._jobs[j.id]

    # -- API ------------------------------------------------------------
    def find_action(self, name):
        """The action `name` names: exact match first, then case-insensitive
        (test steps are typed by people; the canonical names are uppercase)."""
        actions = self.plugin.actions()
        if name in actions:
            return actions[name]
        for key, action in actions.items():
            if key.lower() == name.lower():
                return action
        return None

    def start(self, body: dict) -> Job:
        if not isinstance(body, dict):
            raise HttpError(400, "bad_request", "body must be a JSON object")
        name = body.get("action")
        if not isinstance(name, str):
            raise HttpError(400, "bad_request", "missing 'action'")
        action = self.find_action(name)
        if action is None:
            known = ", ".join(sorted(self.plugin.actions()))
            raise HttpError(404, "unknown_action", f"unknown action {name!r} (actions: {known})")
        used = []

        def resolve(value):
            path = self.plugin.resolve_path(value)
            used.append(path)
            return path
        raw_params = body.get("params") or {}
        params = validate_params(action, raw_params, resolve)
        # The job echoes `upload:<id>` refs, not the host paths they resolved to.
        shown = {k: (raw_params[k] if action.params[k].type == "path" and raw_params.get(k)
                     else v) for k, v in params.items()}

        timeout_ms = body.get("timeout_ms")
        if timeout_ms is None:
            timeout_ms = action.default_timeout_ms
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise HttpError(400, "bad_request", "timeout_ms must be a positive integer")
        if timeout_ms > MAX_TIMEOUT_MS:
            raise HttpError(400, "bad_request", f"timeout_ms must be at most {MAX_TIMEOUT_MS}")

        device = body.get("device")
        if device is not None and not isinstance(device, str):
            raise HttpError(400, "bad_request", "device must be a string")
        if device:
            # This plugin has no devices; the contract lets `device` be omitted.
            raise HttpError(404, "unknown_device",
                            f"{self.plugin.name} has no devices; omit 'device'")

        job = Job(id="j-" + uuid.uuid4().hex[:12], device=None, action=action.name,
                  params=shown, timeout_ms=timeout_ms, uploads=used)
        job.resolved = params
        with self._cond:
            self._prune()
            self._jobs[job.id] = job
        job.ctx = JobContext(job, self)
        threading.Thread(target=self._run, args=(job, action), daemon=True,
                         name=f"job-{job.id}").start()
        threading.Thread(target=self._watchdog, args=(job,), daemon=True).start()
        return job

    def _watchdog(self, job: Job) -> None:
        # Ends the job on time even if the action is stuck in a slow loop.
        while job.state not in TERMINAL_STATES:
            left = job.ctx.remaining()
            if left <= 0:
                job.ctx.cancel_event.set()
                self.append_log(job, f"timeout after {job.timeout_ms} ms")
                self._set_state(job, "timeout", error={
                    "code": "timeout", "message": f"no result within {job.timeout_ms} ms"})
                return
            with self._cond:
                self._cond.wait(min(left, 1.0))

    def _acquire_slot(self, job: Job) -> bool:
        while True:
            if job.ctx.cancel_event.is_set() or job.ctx.remaining() <= 0:
                return False
            if self._slots.acquire(timeout=0.2):
                return True

    def _run(self, job: Job, action: Action) -> None:
        try:
            if not self._acquire_slot(job):
                return  # cancelled or timed out while queued; state set elsewhere
            try:
                self._set_state(job, "running", started_ms=now_ms())
                self._run_action(job, action)
            finally:
                self._slots.release()
        finally:
            # The plugin keeps nothing between jobs: the job's uploads go now.
            self.plugin.release_uploads(job.uploads)

    def _run_action(self, job: Job, action: Action) -> None:
        try:
            result = action.run(job.ctx, dict(job.resolved))
            self._set_state(job, "passed", result=result if result is not None else {})
        except ActionFailed as e:
            self.append_log(job, f"FAILED: {e}")
            self._set_state(job, "failed", result=e.result,
                            error={"code": e.code, "message": str(e)})
        except ActionError as e:
            self.append_log(job, f"ERROR: {e}")
            self._set_state(job, "error", result=e.result,
                            error={"code": e.code, "message": str(e)})
        except JobTimeout:
            self._set_state(job, "timeout", error={
                "code": "timeout", "message": f"no result within {job.timeout_ms} ms"})
        except JobCancelled:
            self._set_state(job, "cancelled", error={"code": "cancelled", "message": "cancelled"})
        except Exception as e:  # noqa: BLE001 - plugin bug, report not crash
            # The traceback goes to the plugin's own log, never to API clients.
            print(f"job {job.id} ({job.action}) crashed:", file=sys.stderr, flush=True)
            traceback.print_exc()
            self.append_log(job, "internal plugin error (details in the plugin's log)")
            self._set_state(job, "error", error={
                "code": "internal",
                "message": f"internal plugin error ({type(e).__name__}); see the plugin's log"})

    def get(self, job_id: str, wait_s: float = 0.0) -> Job:
        if not security.is_job_id(job_id):
            raise HttpError(404, "not_found", "unknown job (ids look like j-<12 hex>)")
        end = time.monotonic() + max(0.0, min(wait_s, MAX_WAIT_S))
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None:
                raise HttpError(404, "not_found", f"unknown job {job_id!r}")
            while job.state not in TERMINAL_STATES:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            return job

    def cancel(self, job_id: str) -> Job:
        if not security.is_job_id(job_id):
            raise HttpError(404, "not_found", "unknown job (ids look like j-<12 hex>)")
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None:
                raise HttpError(404, "not_found", f"unknown job {job_id!r}")
        if job.state not in TERMINAL_STATES:
            job.ctx.cancel_event.set()
            self.append_log(job, "cancel requested")
            if job.state == "queued":
                self._set_state(job, "cancelled",
                                error={"code": "cancelled", "message": "cancelled"})
            # A running action notices the event at its next ctx.check(); give it a
            # moment, then end the job regardless so callers aren't left waiting.
            self.get(job_id, 2.0)
            self._set_state(job, "cancelled", error={"code": "cancelled", "message": "cancelled"})
        return job


# ───────────────────────────── uploads ─────────────────────────────


class Uploads:
    """POST /uploads: a PNG (stored as <dir>/upload.png) or a baselines zip
    (validated, unpacked under <dir>/files/). Short-lived: an upload is deleted
    when a job that used it ends, and after UPLOAD_TTL_S otherwise.

    Hardening: the body is capped (MAX_UPLOAD_BYTES, else 413); a zip may hold
    only `snapshots/<android|ios>/<W>x<H>@<scale>x/<name>.png` files (and the
    directories leading to them) — any other entry, `..`, an absolute path,
    NUL, a backslash or a symlink entry refuses the whole upload (400); each
    file must be a PNG of at most MAX_ZIP_MEMBER_BYTES, the count and the
    unpacked total are capped (counted while inflating, not trusted from the
    headers); every target is re-checked with realpath + commonpath. The root
    is created 0700 and must be a real directory owned by this user."""

    def __init__(self, root: str | None = None):
        self.root = root or os.path.join(tempfile.gettempdir(), "mt-diff-uploads")
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        st = os.lstat(self.root)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"upload dir {self.root} is not a real directory")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise RuntimeError(f"upload dir {self.root} belongs to another user; "
                               "set PLUGIN_UPLOAD_DIR to a directory of your own")
        self._lock = threading.Lock()

    def _prune(self) -> None:
        cutoff = time.time() - UPLOAD_TTL_S
        for name in os.listdir(self.root):
            p = os.path.join(self.root, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass

    def live_count(self) -> int:
        return sum(1 for n in os.listdir(self.root) if security.is_upload_id(n))

    def _new_dir(self) -> tuple[str, str]:
        with self._lock:
            self._prune()
            if self.live_count() >= MAX_LIVE_UPLOADS:
                raise HttpError(429, "too_many_uploads",
                                f"{MAX_LIVE_UPLOADS} uploads are waiting to be used; "
                                "start jobs with them or wait for them to expire")
            uid = uuid.uuid4().hex[:12]
            dest = os.path.join(self.root, uid)
            os.makedirs(dest, mode=0o700)
        return uid, dest

    def store(self, data: bytes) -> tuple[str, str]:
        """Store a PNG or a baselines zip; the kind is sniffed from the bytes.
        Returns ("upload:<id>", host path)."""
        if data.startswith(PNG_MAGIC):
            return self.store_png(data)
        if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
            return self.store_zip(data)
        raise HttpError(400, "bad_request",
                        "upload must be a PNG (image/png) or a zip of snapshots/ (application/zip)")

    def store_png(self, data: bytes) -> tuple[str, str]:
        uid, dest = self._new_dir()
        target = os.path.join(dest, UPLOAD_PNG_NAME)
        try:
            with open(target, "xb") as out:
                out.write(data)
        except OSError:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return "upload:" + uid, target

    def store_zip(self, data: bytes) -> tuple[str, str]:
        uid, dest = self._new_dir()
        files_dir = os.path.join(dest, UPLOAD_ZIP_DIR)
        os.makedirs(files_dir, mode=0o700)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                infos = zf.infolist()
                if len(infos) > MAX_ZIP_ENTRIES:
                    raise HttpError(413, "too_large",
                                    f"zip has more than {MAX_ZIP_ENTRIES} entries")
                base = os.path.realpath(files_dir)
                files = [i for i in infos if not i.is_dir()]
                if len(files) > MAX_ZIP_FILES:
                    raise HttpError(413, "too_large",
                                    f"zip has more than {MAX_ZIP_FILES} baselines")
                # Validate every name before writing anything.
                for info in infos:
                    try:
                        security.check_zip_member(info.filename, info.is_dir(),
                                                  stat.S_ISLNK(info.external_attr >> 16))
                    except ValueError as e:
                        raise HttpError(400, "bad_request", str(e)) from None
                    target = os.path.realpath(os.path.join(files_dir, info.filename))
                    if os.path.commonpath([target, base]) != base:
                        raise HttpError(400, "bad_request",
                                        f"zip entry escapes the upload dir: {info.filename!r}")
                total = 0
                for info in files:
                    target = os.path.join(files_dir, info.filename)
                    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
                    size = 0
                    with zf.open(info) as src, open(target, "xb") as out:
                        while True:
                            chunk = src.read(1 << 16)
                            if not chunk:
                                break
                            if size == 0 and not chunk.startswith(PNG_MAGIC):
                                raise HttpError(400, "bad_request",
                                                f"zip entry {info.filename!r} is not a PNG")
                            size += len(chunk)
                            total += len(chunk)
                            if size > MAX_ZIP_MEMBER_BYTES:
                                raise HttpError(413, "too_large",
                                                f"zip entry {info.filename!r} is larger than "
                                                f"{MAX_ZIP_MEMBER_BYTES} bytes")
                            if total > MAX_UNZIPPED_BYTES:
                                raise HttpError(413, "too_large",
                                                f"zip unpacks to more than "
                                                f"{MAX_UNZIPPED_BYTES} bytes")
                            out.write(chunk)
                    if size == 0:
                        raise HttpError(400, "bad_request",
                                        f"zip entry {info.filename!r} is empty")
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError,
                RuntimeError, ValueError):
            shutil.rmtree(dest, ignore_errors=True)
            raise HttpError(400, "bad_request", "body is not a usable zip archive") from None
        except (HttpError, OSError):
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return "upload:" + uid, files_dir

    def resolve(self, ref: str) -> str:
        """`upload:<id>` -> that upload's directory (400 if unknown/expired)."""
        uid = ref[len("upload:"):]
        if not security.is_upload_id(uid):
            raise HttpError(400, "bad_request", "bad upload reference (upload:<12 hex>)")
        p = os.path.join(self.root, uid)
        if not os.path.isdir(p) or os.path.islink(p):
            raise HttpError(400, "bad_request",
                            f"upload {ref!r} doesn't exist (expired, or already used by a job: "
                            "upload once per job)")
        return p

    def release(self, paths) -> None:
        """Delete upload dirs (as returned by resolve) once their job is done."""
        for p in paths or ():
            if (isinstance(p, str) and security.is_upload_id(os.path.basename(p))
                    and os.path.dirname(os.path.realpath(p)) == os.path.realpath(self.root)):
                shutil.rmtree(p, ignore_errors=True)


# ───────────────────────────── plugin interface ─────────────────────────────


class Plugin:
    """What a concrete plugin implements. See diff_plugin.DiffPlugin."""

    name = "PLUGIN"
    default_name = None
    version = "0.0.0"
    capabilities: list = []

    def __init__(self, upload_dir: str | None = None):
        self.uploads = Uploads(upload_dir)

    def actions(self) -> dict:  # name -> Action
        raise NotImplementedError

    def devices(self) -> list:  # list of device JSON dicts
        return []

    def device_error(self):
        return None

    def resolve_path(self, value: str) -> str:
        """A `path` param: only `upload:<id>` refs. The plugin is stateless and
        may run on another host, so it reads no host paths from requests."""
        if value.startswith("upload:"):
            return self.uploads.resolve(value)
        raise HttpError(400, "bad_request",
                        "path params take an upload reference (upload:<id> from POST /uploads); "
                        "host paths are not accepted")

    def release_uploads(self, paths) -> None:
        self.uploads.release(paths)


# ───────────────────────────── HTTP ─────────────────────────────


def token_matches(expected: str | None, given: str | None) -> bool:
    """Constant-time X-Plugin-Token check. No expected token: everything matches."""
    if not expected:
        return True
    return hmac.compare_digest((given or "").encode("utf-8"), expected.encode("utf-8"))


def make_handler(plugin: Plugin, jobs: JobManager, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"mt-plugin-{plugin.name}/{plugin.version}"
        protocol_version = "HTTP/1.1"
        # Socket timeout per connection: a client that stops sending mid-request
        # frees its thread. Long-poll waits don't read the socket.
        timeout = HANDLER_SOCKET_TIMEOUT_S

        def log_message(self, fmt, *args):  # quieter default logging
            if os.environ.get("PLUGIN_ACCESS_LOG"):
                super().log_message(fmt, *args)

        # -- helpers --
        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, e: HttpError) -> None:
            if not self._body_done:
                # An unread request body would be parsed as the next request on
                # this keep-alive connection: answer, then close it.
                self.close_connection = True
            self._send(e.status, {"error": str(e), "code": e.code})

        def _authorized(self) -> bool:
            return token_matches(token, self.headers.get("X-Plugin-Token"))

        def _auth(self) -> None:
            if not self._authorized():
                raise HttpError(401, "unauthorized", "missing or wrong X-Plugin-Token")

        def _body(self, limit: int = MAX_JSON_BYTES) -> bytes:
            """The request body: Content-Length required for a body (no chunked
            encoding), capped at `limit` (413 above it), bad lengths are 400."""
            if self.headers.get("Transfer-Encoding"):
                raise HttpError(411, "length_required",
                                "send a Content-Length (chunked bodies are not accepted)")
            raw_len = self.headers.get("Content-Length")
            if raw_len is None or raw_len.strip() == "":
                self._body_done = True
                return b""
            try:
                n = int(raw_len.strip())
            except ValueError:
                raise HttpError(400, "bad_request", "bad Content-Length") from None
            if n < 0:
                raise HttpError(400, "bad_request", "bad Content-Length")
            if n > limit:
                raise HttpError(413, "too_large", f"body too large (max {limit} bytes)")
            data = self.rfile.read(n) if n else b""
            self._body_done = True
            if len(data) != n:
                raise HttpError(400, "bad_request", "body shorter than its Content-Length")
            return data

        def _json_body(self):
            raw = self._body(MAX_JSON_BYTES)
            try:
                return json.loads(raw or b"{}")
            except (ValueError, RecursionError):  # JSONDecodeError, bad UTF-8, deep nesting
                raise HttpError(400, "bad_request", "body is not valid JSON") from None

        def _dispatch(self, method: str) -> None:
            raw_len = (self.headers.get("Content-Length") or "").strip()
            self._body_done = raw_len in ("", "0") and not self.headers.get("Transfer-Encoding")
            try:
                url = urlparse(self.path)
                parts = [p for p in url.path.split("/") if p]
                # /health is open (the viewer's liveness check); everything else
                # needs the token when one is configured.
                if not (method == "GET" and parts == ["health"]):
                    self._auth()
                query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                self._route(method, parts, query)
            except HttpError as e:
                self._error(e)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                self.close_connection = True
            except Exception:  # noqa: BLE001 - details to the log, not the client
                traceback.print_exc()
                try:
                    self._error(HttpError(500, "internal",
                                          "internal plugin error; see the plugin's log"))
                except OSError:
                    pass

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        # -- routes --
        def _route(self, method, parts, query):
            if method == "GET" and parts == ["health"]:
                # Unauthenticated: contract, plugin name/version, capabilities.
                # Never the token, env or file paths; the host name only to
                # authenticated callers.
                info = {"name": plugin.name, "version": plugin.version}
                if plugin.default_name:
                    info["default_name"] = plugin.default_name
                body = {
                    "ok": plugin.device_error() is None,
                    "contract": CONTRACT,
                    "contract_version": CONTRACT_VERSION,
                    "plugin": info,
                    "devices": len(plugin.devices()),
                    "capabilities": list(plugin.capabilities),
                    "error": plugin.device_error(),
                }
                if readme_available():
                    body["docs"] = DOCS_PATH
                if self._authorized():
                    body["host"] = {"os": platform.system().lower(),
                                    "hostname": socket.gethostname()}
                return self._send(200, body)
            if method == "GET" and parts == ["docs"]:
                return self._docs()
            if method == "GET" and parts == ["devices"]:
                return self._send(200, {"devices": plugin.devices(),
                                        "error": plugin.device_error()})
            if method == "GET" and parts == ["actions"]:
                return self._send(200, {"actions": [a.to_json()
                                                    for a in plugin.actions().values()]})
            if method == "POST" and parts == ["jobs"]:
                body = self._json_body()
                job = jobs.start(body)
                return self._send(202, {"job": job.to_json()})
            if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
                try:
                    wait = float(query.get("wait", "0"))
                except ValueError:
                    raise HttpError(400, "bad_request", "wait must be a number") from None
                if not 0 <= wait <= MAX_WAIT_S:
                    raise HttpError(400, "bad_request", f"wait must be 0-{MAX_WAIT_S}")
                job = jobs.get(parts[1], wait)
                return self._send(200, job.to_json())
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                return self._send(200, {"job": jobs.cancel(parts[1]).to_json()})
            if method == "POST" and parts == ["uploads"] and "uploads" in plugin.capabilities:
                if self.headers.get("Content-Length") is None:
                    raise HttpError(411, "length_required", "POST /uploads needs a Content-Length")
                data = self._body(MAX_UPLOAD_BYTES)
                if not data:
                    raise HttpError(400, "bad_request", "empty upload")
                ref, path = plugin.uploads.store(data)
                return self._send(201, {"upload": ref, "path": path})
            raise HttpError(404, "not_found", f"no route for {method} {self.path}")

        def _docs(self):
            # Authenticated like every route but /health (see _dispatch).
            try:
                with open(README_PATH, encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                raise HttpError(404, "not_found", "no documentation (README.md missing)") from None
            body = docs_html(plugin.name, text, plugin.version)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", DOCS_CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class PluginServer:
    """The HTTP server. Refuses (ValueError) a non-loopback `host` without a
    token, the same rule main() enforces at startup (security.bind_policy_error)."""

    def __init__(self, plugin: Plugin, host: str = security.DEFAULT_BIND, port: int = 0,
                 token: str | None = None):
        err = security.bind_policy_error(host, token)
        if err:
            raise ValueError(err)
        self.plugin = plugin
        self.jobs = JobManager(plugin)
        server_cls = ThreadingHTTPServer
        if ":" in host:  # an IPv6 literal such as ::1
            server_cls = type("ThreadingHTTPServer6", (ThreadingHTTPServer,),
                              {"address_family": socket.AF_INET6})
        self.httpd = server_cls((host.strip("[]"), port), make_handler(plugin, self.jobs, token))
        self.httpd.daemon_threads = True

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
