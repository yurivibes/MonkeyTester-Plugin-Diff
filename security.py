"""Bind/token policy and request-input validators for the DIFF plugin.

The bind/token part is the same policy as the other MonkeyTester plugins
(MonkeyTester-Plugin-Esp32, MonkeyTester-Plugin-Server): loopback by default,
a non-loopback bind needs a PLUGIN_TOKEN of at least 16 characters. The
validators below are this plugin's own (upload ids, baseline zip members).

See SECURITY.md for the threat model. Standard library only.
"""

from __future__ import annotations

import ipaddress
import os
import re

# The plugin API listens here unless PLUGIN_BIND says otherwise. Loopback:
# the viewer runs on the same host and reaches the plugin at 127.0.0.1.
DEFAULT_BIND = "127.0.0.1"
# A token guarding a LAN-reachable plugin must not be guessable.
MIN_TOKEN_LEN = 16

JOB_ID_RE = re.compile(r"^j-[0-9a-f]{12}$")
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{12}$")

# A baseline inside an uploaded baselines zip. The same rule as MonkeyTester's
# app.py SNAPSHOT_PATH_RE: snapshots/<platform>/<W>x<H>@<scale>x/<name>.png
SNAPSHOT_PATH_RE = re.compile(
    r"^snapshots/(android|ios)/\d{2,5}x\d{2,5}@\d+(\.\d+)?x/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.png$")
# Directory entries a zip tool may add on the way to those files.
SNAPSHOT_DIR_RE = re.compile(
    r"^snapshots/(?:(?:android|ios)/(?:\d{2,5}x\d{2,5}@\d+(?:\.\d+)?x/)?)?$")


# ───────────────────────────── bind / token ─────────────────────────────


def is_loopback(host: str | None) -> bool:
    """True for 127.0.0.0/8, ::1 and `localhost`; False for anything else,
    including "" and 0.0.0.0 / :: (every interface)."""
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def bind_policy_error(bind: str | None, token: str | None) -> str | None:
    """Why the plugin must refuse to start on `bind` with `token`, or None.

    Loopback bind: a token is optional. Any other bind (a LAN address,
    0.0.0.0, ::): PLUGIN_TOKEN is mandatory and at least MIN_TOKEN_LEN chars."""
    if is_loopback(bind):
        return None
    if not token:
        return (f"refusing to listen on {bind!r} without PLUGIN_TOKEN: a non-loopback "
                "bind exposes the plugin (uploads, comparisons) to the network. "
                "Set PLUGIN_TOKEN (e.g. `openssl rand -hex 32`) or bind to 127.0.0.1.")
    if len(token) < MIN_TOKEN_LEN:
        return (f"refusing to listen on {bind!r}: PLUGIN_TOKEN is shorter than "
                f"{MIN_TOKEN_LEN} characters (use e.g. `openssl rand -hex 32`)")
    return None


def bind_and_token_from_env(env=None) -> tuple[str, str | None]:
    """(bind address, token or None) from PLUGIN_BIND / PLUGIN_TOKEN."""
    env = os.environ if env is None else env
    bind = (env.get("PLUGIN_BIND") or "").strip() or DEFAULT_BIND
    token = (env.get("PLUGIN_TOKEN") or "").strip() or None
    return bind, token


# ───────────────────────────── validators ─────────────────────────────


def is_job_id(value) -> bool:
    return isinstance(value, str) and bool(JOB_ID_RE.match(value))


def is_upload_id(value) -> bool:
    return isinstance(value, str) and bool(UPLOAD_ID_RE.match(value))


def check_zip_member(name: str, is_dir: bool, is_symlink: bool) -> None:
    """Raise ValueError unless `name` may sit in a baselines zip: a baseline
    path matching SNAPSHOT_PATH_RE, or a directory on the way to one. NUL,
    backslashes, absolute paths, `..`/`.` segments and symlinks are refused."""
    if not isinstance(name, str) or not name:
        raise ValueError("empty zip entry name")
    if "\0" in name or "\\" in name:
        raise ValueError(f"zip entry {name!r} contains NUL or a backslash")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ValueError(f"zip entry {name!r} has an absolute path")
    if any(seg in ("..", ".") for seg in name.split("/")):
        raise ValueError(f"zip entry {name!r} has a `..` or `.` segment")
    if is_symlink:
        raise ValueError(f"zip entry {name!r} is a symlink")
    if is_dir:
        if not SNAPSHOT_DIR_RE.match(name if name.endswith("/") else name + "/"):
            raise ValueError(f"zip directory {name!r} is not under "
                             "snapshots/<platform>/<W>x<H>@<scale>x/")
        return
    if not SNAPSHOT_PATH_RE.match(name):
        raise ValueError(f"zip entry {name!r} is not "
                         "snapshots/<android|ios>/<W>x<H>@<scale>x/<name>.png")


def within(path: str, root: str) -> bool:
    """True when realpath(path) is root itself or inside realpath(root)."""
    real, base = os.path.realpath(path), os.path.realpath(root)
    try:
        return os.path.commonpath([real, base]) == base
    except ValueError:  # different drives / mix of absolute and relative
        return False
