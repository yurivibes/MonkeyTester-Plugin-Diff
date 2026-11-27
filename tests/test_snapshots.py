"""SNAPSHOT and ASSERT_LAYOUT compare code: PNG codec, screen classes, masks,
match/mismatch/missing/recorded and the layout checks.

Ported from MonkeyTester `tests/test_snapshots.py` (PngTests, GeometryTests,
ArgsTests, TakeTests, LayoutTests). The runner/viewer/upload tests stay in
MonkeyTester; TakeTests changed with take(): it returns PNG bytes instead of
writing files.

Run: python3 -m unittest discover -s tests
"""
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import layout  # noqa: E402
import snapshots  # noqa: E402


def solid(w, h, rgb=(200, 200, 200)):
    return bytes(rgb) * (w * h)


def png(w, h, rgb=(200, 200, 200), pixels=None):
    return snapshots.encode_png(w, h, pixels if pixels is not None else solid(w, h, rgb))


def paint(pixels, w, x0, y0, x1, y1, rgb=(0, 0, 0)):
    out = bytearray(pixels)
    for y in range(y0, y1):
        for x in range(x0, x1):
            out[(y * w + x) * 3:(y * w + x) * 3 + 3] = bytes(rgb)
    return bytes(out)


def raw_png(width, height, color, raw):
    def chunk(t, body):
        return struct.pack(">I", len(body)) + t + body + struct.pack(">I", zlib.crc32(t + body))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class PngTests(unittest.TestCase):
    def test_round_trip_and_rgba(self):
        pixels = paint(solid(7, 5), 7, 1, 1, 3, 4, (10, 20, 30))
        self.assertEqual(snapshots.decode_png(snapshots.encode_png(7, 5, pixels)), (7, 5, pixels))
        # An RGBA PNG with every filter type decodes to the same RGB.
        rgba = bytearray()
        for i in range(0, len(pixels), 3):
            rgba += pixels[i:i + 3] + b"\xff"
        stride = 7 * 4
        raw = bytearray()
        prev = bytearray(stride)
        for y in range(5):
            line = rgba[y * stride:(y + 1) * stride]
            ftype = y % 5
            enc = bytearray(stride)
            for i in range(stride):
                a = line[i - 4] if i >= 4 else 0
                b = prev[i]
                c = prev[i - 4] if i >= 4 else 0
                if ftype == 0:
                    pred = 0
                elif ftype == 1:
                    pred = a
                elif ftype == 2:
                    pred = b
                elif ftype == 3:
                    pred = (a + b) >> 1
                else:
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                enc[i] = (line[i] - pred) & 0xFF
            raw += bytes([ftype]) + enc
            prev = line
        self.assertEqual(snapshots.decode_png(raw_png(7, 5, 6, bytes(raw))), (7, 5, pixels))

    def test_not_a_png(self):
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.decode_png(b"GIF89a")

    def test_hostile_pngs_are_refused(self):
        # Truncated file, oversized header (zlib bomb guard), corrupt and short data.
        good = png(4, 4)
        bad = [good[:30],
               raw_png(100000, 100000, 2, b"\x00"),
               raw_png(4, 4, 2, b"\x00" * 5),
               good[:33] + struct.pack(">I4s", 5, b"IDAT") + b"xxxxx" + b"\0\0\0\0"]
        for data in bad:
            with self.assertRaises(snapshots.SnapshotError):
                snapshots.decode_png(data)

    def test_png_size(self):
        self.assertEqual(snapshots.png_size(png(9, 4)), (9, 4))
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.png_size(b"nope")


class GeometryTests(unittest.TestCase):
    def test_screen_class_is_points_or_dp_plus_scale(self):
        self.assertEqual(snapshots.screen_class("ios", 750, 1334, 2.0), "ios/375x667@2x")
        self.assertEqual(snapshots.screen_class("ios", 1179, 2556, 3.0), "ios/393x852@3x")
        self.assertEqual(snapshots.screen_class("android", 1080, 2388, 2.625),
                         "android/411x910@2.625x")

    def test_downscale_averages_boxes(self):
        # 4x2 -> 2x1: left box black/white average, right box solid.
        px = bytes([0, 0, 0, 255, 255, 255, 50, 50, 50, 50, 50, 50,
                    0, 0, 0, 255, 255, 255, 50, 50, 50, 50, 50, 50])
        self.assertEqual(snapshots.downscale(4, 2, px, 2, 1), bytes([128] * 3 + [50] * 3))


class ArgsTests(unittest.TestCase):
    def test_masks_and_tolerance(self):
        name, masks, tol = snapshots.parse_args(
            "home mask=status_bar,tag:clock,rect:0,10,20.5,30 tolerance=0.5")
        self.assertEqual(name, "home")
        self.assertEqual(masks, [("status_bar",), ("tag", "clock"), ("rect", 0, 10, 20.5, 30)])
        self.assertEqual(tol, 0.5)
        self.assertTrue(snapshots.needs_elements(masks))
        self.assertFalse(snapshots.needs_elements([("status_bar",)]))

    def test_bad_arguments(self):
        for args in ("", "../x", "home mask=moon", "home tolerance=abc", "home colour=red",
                     "home mask=rect:1,2", "home tolerance=101"):
            with self.assertRaises(snapshots.SnapshotError, msg=args):
                snapshots.parse_args(args)


class TakeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "snapshots"
        # A 40x60 px phone at scale 2 -> 20x30 @2x.
        self.capture = png(40, 60)

    def take(self, capture=None, **kw):
        return snapshots.take("home", capture_png=capture or self.capture, platform="ios",
                              scale=2.0, snapshot_root=self.root, **kw)

    def baseline(self, cls="ios/20x30@2x", pixels=None, w=20, h=30):
        path = self.root / cls / "home.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(snapshots.encode_png(w, h, pixels or solid(w, h)))

    def test_missing_baseline_fails_and_lists_what_exists(self):
        r = self.take()
        self.assertEqual(r["status"], "missing")
        self.assertIn("snapshots/ios/20x30@2x/home.png", r["message"])
        self.assertIn("no baselines for this snapshot yet", r["message"])
        self.assertEqual(snapshots.decode_png(r["actual_png"])[:2], (20, 30))
        self.assertIsNone(r["diff_png"])
        self.baseline("ios/375x667@2x")
        r = self.take()
        self.assertIn("baselines exist for: ios/375x667@2x", r["message"])
        self.assertEqual(r["available_classes"], ["ios/375x667@2x"])

    def test_no_snapshot_root_is_missing(self):
        r = snapshots.take("home", capture_png=self.capture, platform="ios", scale=2.0,
                           snapshot_root=None)
        self.assertEqual((r["status"], r["available_classes"]), ("missing", []))

    def test_record_mode_returns_capture_without_comparing(self):
        self.baseline(pixels=solid(20, 30, (0, 0, 0)))
        r = self.take(record=True)
        self.assertEqual(r["status"], "recorded")
        self.assertEqual(snapshots.decode_png(r["actual_png"])[:2], (20, 30))
        self.assertIsNone(r["diff_percent"])
        self.assertIsNone(r["diff_png"])

    def test_match_mismatch_and_masks(self):
        self.baseline()
        r = self.take()
        self.assertEqual(r["status"], "match")
        self.assertEqual(r["baseline_path"], "ios/20x30@2x/home.png")
        self.assertEqual(r["diff_percent"], 0.0)
        self.assertEqual(snapshots.decode_png(r["diff_png"])[:2], (20, 30))
        changed = png(40, 60, pixels=paint(solid(40, 60), 40, 0, 0, 40, 4))  # top 2pt rows
        r = self.take(changed)
        self.assertEqual(r["status"], "mismatch")
        self.assertAlmostEqual(r["diff_percent"], 100 * 20 * 2 / 600, places=3)
        self.assertIsNotNone(r["diff_png"])
        self.assertEqual(self.take(changed, masks=[("rect", 0, 0, 20, 2)])["status"], "match")
        self.assertEqual(self.take(changed, tolerance=10)["status"], "match")

    def test_tag_mask_uses_element_pixels(self):
        self.baseline()
        changed = png(40, 60, pixels=paint(solid(40, 60), 40, 10, 10, 20, 20))
        elements = [{"id": "clock", "x": 10, "y": 10, "w": 10, "h": 10}]
        self.assertEqual(self.take(changed, masks=[("tag", "clock")], elements=elements)["status"],
                         "match")
        with self.assertRaises(snapshots.SnapshotError):
            self.take(changed, masks=[("tag", "nope")], elements=elements)

    def test_size_mismatch_fails(self):
        self.baseline(w=20, h=31, pixels=solid(20, 31))
        r = self.take()
        self.assertEqual(r["status"], "mismatch")
        self.assertIn("20x31", r["message"])
        self.assertIsNone(r["diff_png"])

    def test_status_bar_mask_depends_on_screen_shape(self):
        self.assertEqual(snapshots.mask_rects([("status_bar",)], "ios", 375, 667, 2), [(0, 0, 375, 20)])
        self.assertEqual(snapshots.mask_rects([("status_bar",), ("nav_bar",)], "ios", 393, 852, 3),
                         [(0, 0, 393, 59), (0, 818, 393, 852)])
        self.assertEqual(snapshots.mask_rects([("nav_bar",)], "ios", 375, 667, 2), [])

    def test_check_hook_can_stop_the_compare(self):
        self.baseline()

        class Stop(Exception):
            pass

        def check():
            raise Stop()
        with self.assertRaises(Stop):
            self.take(check=check)


SCREEN = {"width": 750, "height": 1334, "scale": 2.0}  # 375x667 pt


def el(tag, x, y, w, h):  # pt -> device px at scale 2
    return {"id": tag, "x": x * 2, "y": y * 2, "w": w * 2, "h": h * 2}


ELEMENTS = [el("logo", 137.5, 100, 100, 50), el("title", 40, 170, 295, 30),
            el("agree", 40, 600, 295, 44), el("terms", 20, 190, 335, 400)]


class LayoutTests(unittest.TestCase):
    def check(self, args):
        return layout.check(args, SCREEN, ELEMENTS)

    def test_passing_checks(self):
        for args in ("logo visible", "logo centered-x", "logo inside screen",
                     "title below logo gap>=10 gap<=30", "logo above title",
                     "title no-overlap agree", "title aligned-left agree",
                     "agree aligned-center-x logo", "title inside terms tol=40",
                     "logo centered-x tol=1%"):
            ok, msg = self.check(args)
            self.assertTrue(ok, f"{args}: {msg}")

    def test_failing_checks(self):
        for args in ("title centered-y", "logo below title", "title below logo gap>=30",
                     "terms no-overlap title", "missing visible", "title aligned-left terms"):
            ok, msg = self.check(args)
            self.assertFalse(ok, f"{args}: {msg}")
        self.assertIn("tags on screen", self.check("missing visible")[1])

    def test_bad_syntax(self):
        for args in ("logo", "logo spins", "logo below", "logo below title gap=3", "logo visible tol>=2"):
            with self.assertRaises(layout.LayoutError, msg=args):
                self.check(args)


if __name__ == "__main__":
    unittest.main()
