"""Security hardening tests: bind/token policy, request caps, upload
validation (zip members, sizes, kinds) and identifiers.

Run: python3 -m unittest discover -s tests
"""

import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import plugin_server as ps  # noqa: E402
import security  # noqa: E402
from plugin_server import PluginServer  # noqa: E402
from test_plugin import Harness, make_zip  # noqa: E402
from test_snapshots import ELEMENTS, SCREEN, png  # noqa: E402

TOKEN = "0123456789abcdef-test-token"
GOOD = "snapshots/ios/375x667@2x/home.png"


def scrubbed_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGIN_")}
    env.update({"PLUGIN_UPLOAD_DIR": os.path.join(tempfile.mkdtemp(), "uploads"),
                "PLUGIN_PORT": "0"})
    env.update(extra)
    return env


class BindPolicyTests(unittest.TestCase):
    def test_loopback_detection(self):
        for h in ("127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST"):
            self.assertTrue(security.is_loopback(h), h)
        for h in ("0.0.0.0", "", None, "::", "192.168.125.70", "example.com"):  # nosec B104
            self.assertFalse(security.is_loopback(h), h)

    def test_defaults_are_loopback_without_token(self):
        self.assertEqual(security.DEFAULT_BIND, "127.0.0.1")
        self.assertEqual(security.bind_and_token_from_env({}), ("127.0.0.1", None))
        self.assertEqual(security.bind_and_token_from_env({"PLUGIN_BIND": " ", "PLUGIN_TOKEN": " "}),
                         ("127.0.0.1", None))

    def test_policy(self):
        self.assertIsNone(security.bind_policy_error("127.0.0.1", None))
        self.assertIsNone(security.bind_policy_error("::1", "short"))
        self.assertIn("PLUGIN_TOKEN", security.bind_policy_error("0.0.0.0", None))  # nosec B104
        self.assertIn("PLUGIN_TOKEN", security.bind_policy_error("192.168.1.5", None))
        self.assertIn("shorter", security.bind_policy_error("192.168.1.5", "short"))
        self.assertIsNone(security.bind_policy_error("192.168.1.5", TOKEN))

    def test_plugin_server_refuses_non_loopback_without_token(self):
        with self.assertRaises(ValueError):
            PluginServer(object(), "0.0.0.0", 0, None)  # nosec B104 - must be refused

    def _run_main(self, **env):
        return subprocess.run([sys.executable, os.path.join(ROOT, "diff_plugin.py")],
                              env=scrubbed_env(**env), capture_output=True, text=True,
                              timeout=30, check=False)

    def test_main_exits_non_zero_on_lan_bind_without_token(self):
        for bind in ("0.0.0.0", "192.168.125.70"):  # nosec B104 - must be refused
            r = self._run_main(PLUGIN_BIND=bind)
            self.assertEqual(r.returncode, 2, r)
            self.assertIn("PLUGIN_TOKEN", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_main_exits_non_zero_on_short_token(self):
        r = self._run_main(PLUGIN_BIND="0.0.0.0", PLUGIN_TOKEN="abc")  # nosec B104
        self.assertEqual(r.returncode, 2, r)
        self.assertIn("shorter", r.stderr)

    def test_main_listens_on_loopback_by_default(self):
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "diff_plugin.py")],
                                env=scrubbed_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
        finally:
            proc.terminate()
            proc.wait(10)
            proc.stdout.close()
            proc.stderr.close()
        self.assertIn("DIFF", line)
        self.assertIn(" on 127.0.0.1:", line)
        self.assertIn("token off", line)

    def test_default_port_is_8095(self):
        import diff_plugin
        self.assertEqual(diff_plugin.PLUGIN_DEFAULT_PORT, 8095)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(token=TOKEN)
        self.addCleanup(self.h.close)

    def call_as(self, token, method, path, payload=None):
        saved, self.h.token = self.h.token, None
        try:
            headers = {"X-Plugin-Token": token} if token is not None else {}
            return self.h.call(method, path, payload, headers=headers)
        finally:
            self.h.token = saved

    def test_token_required_except_health(self):
        for token in (None, "", "wrong", TOKEN + "x"):
            for method, path in (("GET", "/devices"), ("GET", "/actions"), ("GET", "/docs"),
                                 ("GET", "/jobs/j-000000000000"), ("POST", "/jobs"),
                                 ("POST", "/uploads")):
                code, r = self.call_as(token, method, path, {} if method == "POST" else None)
                self.assertEqual((code, r.get("code")), (401, "unauthorized"), (path, token))
        self.assertEqual(self.h.call("GET", "/actions")[0], 200)

    def test_health_is_open_and_leaks_nothing(self):
        code, body = self.call_as(None, "GET", "/health")
        self.assertEqual(code, 200)
        self.assertNotIn("host", body)
        self.assertNotIn(TOKEN, str(body))
        self.assertNotIn(self.h.plugin.uploads.root, str(body))
        code, body = self.h.call("GET", "/health")
        self.assertIn("host", body)

    def test_compare_digest_is_used(self):
        with mock.patch.object(ps.hmac, "compare_digest", wraps=ps.hmac.compare_digest) as cd:
            self.assertTrue(ps.token_matches(TOKEN, TOKEN))
            self.assertFalse(ps.token_matches(TOKEN, "nope"))
            self.assertEqual(cd.call_count, 2)
        self.assertTrue(ps.token_matches(None, None))


class RequestCapTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_bad_bodies(self):
        status, data, _ = self.h.raw("POST", "/jobs", b"{nope", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, data, _ = self.h.raw("POST", "/jobs", b"[]", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, data, _ = self.h.raw("POST", "/jobs", None,
                                     {"Content-Length": str(ps.MAX_JSON_BYTES + 1)})
        self.assertEqual(status, 413)
        status, data, _ = self.h.raw("POST", "/uploads", None,
                                     {"Content-Length": str(ps.MAX_UPLOAD_BYTES + 1)})
        self.assertEqual(status, 413)

    def test_timeout_ms_capped(self):
        for t in (0, -1, True, "5", ps.MAX_TIMEOUT_MS + 1):
            code, body = self.h.call("POST", "/jobs", {
                "action": "ASSERT_LAYOUT", "timeout_ms": t,
                "params": {"args": "logo visible", "screen": SCREEN, "elements": ELEMENTS}})
            self.assertEqual(code, 400, t)

    def test_no_traceback_to_clients(self):
        with mock.patch.object(self.h.plugin, "actions", side_effect=RuntimeError("secret")), \
                mock.patch.object(ps.traceback, "print_exc"):
            code, body = self.h.call("GET", "/actions")
        self.assertEqual((code, body["code"]), (500, "internal"))
        self.assertNotIn("secret", body["error"])


class UploadValidationTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.root = self.h.plugin.uploads.root

    def refused(self, data, status=400):
        code, body = self.h.upload(data, "application/zip")
        self.assertEqual(code, status, body)
        self.assertEqual(os.listdir(self.root), [], "a refused upload must leave nothing")
        return body

    def test_png_and_zip_accepted(self):
        code, body = self.h.upload(png(4, 4), "image/png")
        self.assertEqual(code, 201)
        self.assertRegex(body["upload"], r"^upload:[0-9a-f]{12}$")
        self.assertTrue(body["path"].endswith("/upload.png"))
        code, body = self.h.upload(make_zip({GOOD: png(4, 4),
                                             "snapshots/android/443x980@2.438x/a.png": png(2, 2)}),
                                   "application/zip")
        self.assertEqual(code, 201, body)
        self.assertTrue(os.path.isfile(os.path.join(body["path"], GOOD)))
        st = os.stat(self.root)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o700)

    def test_directory_entries_allowed(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for d in ("snapshots/", "snapshots/ios/", "snapshots/ios/375x667@2x/"):
                zf.writestr(zipfile.ZipInfo(d), b"")
            zf.writestr(GOOD, png(2, 2))
        self.assertEqual(self.h.upload(buf.getvalue(), "application/zip")[0], 201)

    def test_bad_member_paths_refused(self):
        for name in ("../x.png", "snapshots/../../x.png", "/snapshots/ios/375x667@2x/a.png",
                     "snapshots/ios/home.png", "snapshots/mac/1x1@1x/a.png",
                     "snapshots/ios/375x667@2x/.hidden.png", "snapshots/ios/375x667@2x/a.txt",
                     "home.png", "snapshots/ios/375x667@2x/sub/a.png",
                     "snapshots/ios/375x667@2x/./a.png", "snapshots\\ios\\375x667@2x\\a.png",
                     "__MACOSX/snapshots/ios/375x667@2x/a.png", "snapshots/ios/1x1@2x/a.png"):
            body = self.refused(make_zip({name: png(2, 2)}))
            self.assertEqual(body["code"], "bad_request", name)

    def test_member_must_be_png_and_not_empty(self):
        self.refused(make_zip({GOOD: b"GIF89a"}))
        self.refused(make_zip({GOOD: b""}))

    def test_symlink_member_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo(GOOD)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
        self.refused(buf.getvalue())

    def test_size_and_count_caps(self):
        with mock.patch.object(ps, "MAX_ZIP_MEMBER_BYTES", 100):
            self.refused(make_zip({GOOD: png(40, 40, (1, 2, 3)) + b"\0" * 200}), 413)
        with mock.patch.object(ps, "MAX_ZIP_FILES", 1):
            self.refused(make_zip({GOOD: png(2, 2),
                                   "snapshots/ios/375x667@2x/b.png": png(2, 2)}), 413)
        with mock.patch.object(ps, "MAX_UNZIPPED_BYTES", 100):
            self.refused(make_zip({GOOD: png(2, 2) + b"\0" * 200}), 413)

    def test_zip_bomb_counted_while_inflating(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(GOOD, png(2, 2) + b"\0" * (ps.MAX_ZIP_MEMBER_BYTES + 1))
        self.assertLess(len(buf.getvalue()), 100_000)
        self.refused(buf.getvalue(), 413)

    def test_other_bodies_refused(self):
        code, body = self.h.upload(b"hello", "text/plain")
        self.assertEqual(code, 400)
        self.assertIn("PNG", body["error"])
        code, body = self.h.upload(b"PK\x03\x04garbage", "application/zip")
        self.assertEqual(code, 400)
        self.assertEqual(os.listdir(self.root), [])

    def test_live_upload_cap(self):
        with mock.patch.object(ps, "MAX_LIVE_UPLOADS", 2):
            self.assertEqual(self.h.upload(png(2, 2), "image/png")[0], 201)
            self.assertEqual(self.h.upload(png(2, 2), "image/png")[0], 201)
            code, body = self.h.upload(png(2, 2), "image/png")
        self.assertEqual((code, body["code"]), (429, "too_many_uploads"))

    def test_expired_uploads_are_pruned(self):
        code, body = self.h.upload(png(2, 2), "image/png")
        old = os.path.dirname(body["path"])
        os.utime(old, (0, 0))
        self.h.upload(png(2, 2), "image/png")
        self.assertFalse(os.path.exists(old))

    def test_bad_upload_refs(self):
        for ref in ("upload:../../etc", "upload:XYZ", "upload:", "upload:0123456789ab"):
            code, body = self.h.call("POST", "/jobs", {"action": "SNAPSHOT", "params": {
                "args": "home", "screenshot": ref, "screen": SCREEN, "platform": "ios",
                "baselines": ref}})
            self.assertEqual(code, 400, ref)

    def test_upload_dir_must_be_owned_real_dir(self):
        tmp = tempfile.mkdtemp()
        link = os.path.join(tmp, "link")
        os.symlink(tmp, link)
        with self.assertRaises(RuntimeError):
            ps.Uploads(link)


class ValidatorTests(unittest.TestCase):
    def test_ids(self):
        self.assertTrue(security.is_job_id("j-0123456789ab"))
        self.assertFalse(security.is_job_id("j-0123456789AB"))
        self.assertTrue(security.is_upload_id("0123456789ab"))
        self.assertFalse(security.is_upload_id("../0123456789"))

    def test_snapshot_path_regex_matches_monkeytester(self):
        # The same pattern as MonkeyTester app.py SNAPSHOT_PATH_RE.
        self.assertEqual(security.SNAPSHOT_PATH_RE.pattern,
                         r"^snapshots/(android|ios)/\d{2,5}x\d{2,5}@\d+(\.\d+)?x/"
                         r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.png$")


if __name__ == "__main__":
    unittest.main()
