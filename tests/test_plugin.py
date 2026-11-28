"""Contract and action tests over HTTP: /health, /actions, /devices, uploads,
SNAPSHOT (match / mismatch / missing / recorded / errors), ASSERT_LAYOUT, job
waits, cancel and the delete-after-use rule for uploads.

Run: python3 -m unittest discover -s tests
"""
import base64
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import diff_plugin  # noqa: E402
import plugin_server as ps  # noqa: E402
import snapshots  # noqa: E402
from plugin_server import PluginServer  # noqa: E402
from test_snapshots import ELEMENTS, SCREEN, paint, png, solid  # noqa: E402


class Harness:
    """A DiffPlugin on a random loopback port with a private upload dir."""

    def __init__(self, token=None):
        self.tmp = tempfile.mkdtemp()
        self.plugin = diff_plugin.DiffPlugin(upload_dir=os.path.join(self.tmp, "uploads"))
        self.server = PluginServer(self.plugin, "127.0.0.1", 0, token)
        self.server.start_background()
        self.token = token

    def close(self):
        self.server.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def raw(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=90)
        h = {}
        if self.token:
            h["X-Plugin-Token"] = self.token
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        status, rheaders = resp.status, dict(resp.getheaders())
        conn.close()
        return status, data, rheaders

    def call(self, method, path, payload=None, headers=None):
        body = None
        h = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode()
            h["Content-Type"] = "application/json"
        status, data, _ = self.raw(method, path, body, h)
        return status, json.loads(data or b"{}")

    def upload(self, data, ctype):
        status, body, _ = self.raw("POST", "/uploads", data, {"Content-Type": ctype})
        return status, json.loads(body)

    def run(self, action, params, timeout_ms=None):
        payload = {"action": action, "params": params}
        if timeout_ms:
            payload["timeout_ms"] = timeout_ms
        status, body = self.call("POST", "/jobs", payload)
        if status != 202:
            return status, body
        job = body["job"]
        while job["state"] not in ps.TERMINAL_STATES:
            status, job = self.call("GET", f"/jobs/{job['id']}?wait=30")
        return 200, job


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# A 750x1334 px iPhone at scale 2 -> ios/375x667@2x at 1x.
CLASS = "ios/375x667@2x"


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_health(self):
        code, body = self.h.call("GET", "/health")
        self.assertEqual(code, 200)
        self.assertEqual(body["contract"], "monkeytester-plugin")
        self.assertEqual(body["contract_version"], 1)
        self.assertEqual(body["plugin"]["name"], "DIFF")
        self.assertEqual(body["plugin"]["default_name"], "DIFF")
        self.assertNotIn("renamable", body["plugin"])
        self.assertEqual(body["capabilities"], ["uploads"])
        self.assertEqual(body["docs"], "/docs")
        self.assertTrue(body["ok"])
        self.assertEqual(body["devices"], 0)

    def test_actions_carry_source_params(self):
        code, body = self.h.call("GET", "/actions")
        self.assertEqual(code, 200)
        actions = {a["name"]: a for a in body["actions"]}
        self.assertEqual(sorted(actions), ["ASSERT_LAYOUT", "SNAPSHOT"])
        snap = actions["SNAPSHOT"]["params"]
        self.assertEqual({k: v["source"] for k, v in snap.items()},
                         {"args": "args", "screenshot": "screenshot", "screen": "screen",
                          "platform": "platform", "baselines": "baselines",
                          "elements": "elements", "record": "record"})
        self.assertEqual(snap["screenshot"]["type"], "path")
        self.assertEqual(snap["baselines"]["type"], "path")
        self.assertEqual(snap["platform"]["values"], ["android", "ios"])
        self.assertEqual(snap["elements"]["when"], "tag_mask")
        self.assertFalse(snap["elements"]["required"])
        lay = actions["ASSERT_LAYOUT"]["params"]
        self.assertEqual({k: v["source"] for k, v in lay.items()},
                         {"args": "args", "screen": "screen", "elements": "elements"})
        self.assertTrue(all(v["required"] for v in lay.values()))
        for a in actions.values():
            self.assertFalse(a["needs_device"])

    def test_devices_is_empty_and_device_refused(self):
        self.assertEqual(self.h.call("GET", "/devices"), (200, {"devices": [], "error": None}))
        code, body = self.h.call("POST", "/jobs", {"action": "ASSERT_LAYOUT", "device": "x",
                                                   "params": {"args": "logo visible",
                                                              "screen": SCREEN,
                                                              "elements": ELEMENTS}})
        self.assertEqual((code, body["code"]), (404, "unknown_device"))

    def test_docs_page(self):
        status, data, headers = self.h.raw("GET", "/docs")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn(b"MonkeyTester-Plugin-Diff", data)

    def test_unknown_action_and_case_insensitive_names(self):
        code, body = self.h.call("POST", "/jobs", {"action": "MOTION", "params": {}})
        self.assertEqual((code, body["code"]), (404, "unknown_action"))
        self.assertIn("ASSERT_LAYOUT, SNAPSHOT", body["error"])
        code, job = self.h.run("assert_layout", {"args": "logo visible", "screen": SCREEN,
                                                 "elements": ELEMENTS})
        self.assertEqual((job["state"], job["action"]), ("passed", "ASSERT_LAYOUT"))

    def test_unknown_and_missing_params(self):
        code, body = self.h.call("POST", "/jobs", {"action": "ASSERT_LAYOUT",
                                                   "params": {"args": "a visible", "x": 1}})
        self.assertEqual((code, body["code"]), (400, "bad_request"))
        code, body = self.h.call("POST", "/jobs", {"action": "ASSERT_LAYOUT",
                                                   "params": {"args": "a visible"}})
        self.assertEqual(code, 400)
        self.assertIn("missing required param", body["error"])

    def test_job_not_found_and_bad_wait(self):
        self.assertEqual(self.h.call("GET", "/jobs/j-000000000000")[0], 404)
        self.assertEqual(self.h.call("GET", "/jobs/nope")[0], 404)
        self.assertEqual(self.h.call("GET", "/jobs/j-000000000000?wait=61")[0], 400)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.frame = png(750, 1334)
        self.baseline_1x = png(375, 667)

    def ups(self, frame=None, baselines=None):
        code, shot = self.h.upload(frame or self.frame, "image/png")
        self.assertEqual(code, 201, shot)
        code, zipped = self.h.upload(make_zip(baselines or {}), "application/zip")
        self.assertEqual(code, 201, zipped)
        return shot["upload"], zipped["upload"]

    def snap(self, args, frame=None, baselines=None, **extra):
        shot, zipped = self.ups(frame, baselines)
        params = {"args": args, "screenshot": shot, "screen": SCREEN, "platform": "ios",
                  "baselines": zipped, "record": False}
        params.update(extra)
        return self.h.run("SNAPSHOT", params)

    def assert_shape(self, result):
        self.assertEqual(sorted(result), sorted(
            ["name", "status", "message", "screen_class", "diff_percent", "tolerance",
             "baseline_path", "available_classes", "actual_png_b64", "diff_png_b64"]))

    def test_match(self):
        code, job = self.snap("home mask=status_bar",
                              baselines={f"snapshots/{CLASS}/home.png": self.baseline_1x})
        self.assertEqual(job["state"], "passed", job)
        r = job["result"]
        self.assert_shape(r)
        self.assertEqual((r["name"], r["status"], r["screen_class"], r["baseline_path"]),
                         ("home", "match", CLASS, f"{CLASS}/home.png"))
        self.assertEqual((r["diff_percent"], r["tolerance"]), (0.0, 0.1))
        self.assertEqual(r["available_classes"], [CLASS])
        self.assertEqual(snapshots.decode_png(base64.b64decode(r["actual_png_b64"]))[:2],
                         (375, 667))
        self.assertEqual(snapshots.decode_png(base64.b64decode(r["diff_png_b64"]))[:2],
                         (375, 667))
        self.assertIsNone(job["error"])
        self.assertTrue(job["params"]["screenshot"].startswith("upload:"))

    def test_mismatch(self):
        changed = png(750, 1334, pixels=paint(solid(750, 1334), 750, 0, 200, 750, 400))
        code, job = self.snap("home tolerance=0.5", frame=changed,
                              baselines={f"snapshots/{CLASS}/home.png": self.baseline_1x})
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "mismatch")
        r = job["result"]
        self.assert_shape(r)
        self.assertEqual(r["status"], "mismatch")
        self.assertAlmostEqual(r["diff_percent"], 100 * 100 / 667, places=2)
        self.assertEqual(r["tolerance"], 0.5)
        self.assertIsNotNone(r["diff_png_b64"])

    def test_missing_lists_available_classes(self):
        code, job = self.snap("home", baselines={
            "snapshots/android/443x980@2.438x/home.png": png(443, 980),
            "snapshots/ios/393x852@3x/home.png": png(393, 852),
            f"snapshots/{CLASS}/other.png": self.baseline_1x})
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "missing")
        r = job["result"]
        self.assert_shape(r)
        self.assertEqual(r["status"], "missing")
        self.assertEqual(r["available_classes"], ["android/443x980@2.438x", "ios/393x852@3x"])
        self.assertIn("baseline missing: snapshots/ios/375x667@2x/home.png", r["message"])
        self.assertIn("baselines exist for: android/443x980@2.438x, ios/393x852@3x", r["message"])
        self.assertIsNotNone(r["actual_png_b64"])
        self.assertIsNone(r["diff_png_b64"])

    def test_missing_with_empty_zip(self):
        code, job = self.snap("home")
        self.assertEqual(job["result"]["status"], "missing")
        self.assertIn("no baselines for this snapshot yet", job["result"]["message"])

    def test_recorded(self):
        code, job = self.snap("home", record=True,
                              baselines={f"snapshots/{CLASS}/home.png": png(375, 667, (0, 0, 0))})
        self.assertEqual(job["state"], "passed")
        r = job["result"]
        self.assertEqual(r["status"], "recorded")
        self.assertIsNone(r["diff_percent"])
        self.assertIsNone(r["diff_png_b64"])
        self.assertEqual(base64.b64decode(r["actual_png_b64"]), self.baseline_1x)

    def test_tag_mask_uses_elements_and_needs_them(self):
        changed = png(750, 1334, pixels=paint(solid(750, 1334), 750, 100, 100, 200, 200))
        base = {f"snapshots/{CLASS}/home.png": self.baseline_1x}
        elements = [{"id": "clock", "x": 100, "y": 100, "w": 100, "h": 100}]
        code, job = self.snap("home mask=tag:clock", frame=changed, baselines=base,
                              elements=elements)
        self.assertEqual(job["result"]["status"], "match", job)
        code, job = self.snap("home mask=tag:clock", frame=changed, baselines=base)
        self.assertEqual(job["state"], "error")
        self.assertIn("needs the elements list", job["error"]["message"])

    def test_errors_keep_the_result_shape(self):
        for args, extra, needle in (
                ("../x", {}, "may only use"),
                ("home mask=moon", {}, "unknown mask"),
                ("home", {"screen": {"width": 750, "height": 1334}}, "no 'scale'"),
                ("home", {"screen": {"width": 750, "height": 1334, "scale": 0}}, "more than 0"),
                ("home", {"elements": [{"id": "a"}]}, "elements[0].x")):
            code, job = self.snap(args, **extra)
            self.assertEqual(job["state"], "error", (args, job))
            self.assertEqual(job["error"]["code"], "bad_input")
            self.assertIn(needle, job["error"]["message"])
            self.assert_shape(job["result"])
            self.assertEqual(job["result"]["status"], "error")

    def test_wrong_upload_kinds(self):
        shot, zipped = self.ups()
        params = {"args": "home", "screenshot": zipped, "screen": SCREEN, "platform": "ios",
                  "baselines": shot}
        code, job = self.h.run("SNAPSHOT", params)
        self.assertEqual(job["state"], "error")
        self.assertIn("screenshot must be a PNG upload", job["error"]["message"])

    def test_host_paths_are_refused(self):
        code, body = self.h.call("POST", "/jobs", {"action": "SNAPSHOT", "params": {
            "args": "home", "screenshot": "/etc/passwd", "screen": SCREEN, "platform": "ios",
            "baselines": "/tmp"}})
        self.assertEqual((code, body["code"]), (400, "bad_request"))
        self.assertIn("upload reference", body["error"])

    def test_uploads_are_deleted_after_the_job(self):
        shot, zipped = self.ups(baselines={f"snapshots/{CLASS}/home.png": self.baseline_1x})
        root = self.h.plugin.uploads.root
        self.assertEqual(len(os.listdir(root)), 2)
        params = {"args": "home", "screenshot": shot, "screen": SCREEN, "platform": "ios",
                  "baselines": zipped}
        code, job = self.h.run("SNAPSHOT", params)
        self.assertEqual(job["state"], "passed")
        deadline = time.time() + 5
        while os.listdir(root) and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(os.listdir(root), [])
        # Reusing the refs fails: one upload per job.
        code, body = self.h.call("POST", "/jobs", {"action": "SNAPSHOT", "params": params})
        self.assertEqual(code, 400)
        self.assertIn("already used", body["error"])

    def test_timeout_and_cancel(self):
        big = png(1179, 2556)
        code, shot = self.h.upload(big, "image/png")
        code, zipped = self.h.upload(make_zip({}), "application/zip")
        params = {"args": "home", "screenshot": shot["upload"],
                  "screen": {"width": 1179, "height": 2556, "scale": 3}, "platform": "ios",
                  "baselines": zipped["upload"]}
        code, job = self.h.run("SNAPSHOT", params, timeout_ms=1)
        self.assertEqual(job["state"], "timeout")
        code, shot = self.h.upload(big, "image/png")
        code, zipped = self.h.upload(make_zip({}), "application/zip")
        params.update(screenshot=shot["upload"], baselines=zipped["upload"])
        code, body = self.h.call("POST", "/jobs", {"action": "SNAPSHOT", "params": params})
        code, body = self.h.call("POST", f"/jobs/{body['job']['id']}/cancel", {})
        self.assertEqual(code, 200)
        self.assertIn(body["job"]["state"], ("cancelled", "passed", "failed"))


class LayoutActionTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def layout(self, args, elements=ELEMENTS, screen=SCREEN):
        return self.h.run("ASSERT_LAYOUT", {"args": args, "screen": screen, "elements": elements})

    def test_pass_and_fail(self):
        code, job = self.layout("title below logo gap>=10")
        self.assertEqual(job["state"], "passed")
        self.assertEqual(sorted(job["result"]), ["message", "passed"])
        self.assertTrue(job["result"]["passed"])
        code, job = self.layout("logo below title")
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "layout")
        self.assertFalse(job["result"]["passed"])
        self.assertIn("is not below", job["result"]["message"])

    def test_bad_syntax_and_bad_input_are_errors(self):
        for args, elements, screen in (("logo spins", ELEMENTS, SCREEN),
                                       ("logo visible", "nope", SCREEN),
                                       ("logo visible", ELEMENTS, {"width": 1})):
            code, job = self.layout(args, elements, screen)
            self.assertEqual(job["state"], "error", job)
            self.assertEqual(job["result"]["passed"], False)

    def test_elements_envelope_and_json_strings(self):
        code, job = self.layout("logo visible", {"elements": ELEMENTS})
        self.assertEqual(job["state"], "passed")
        code, job = self.layout("logo visible", json.dumps(ELEMENTS), json.dumps(SCREEN))
        self.assertEqual(job["state"], "passed")


if __name__ == "__main__":
    unittest.main()
