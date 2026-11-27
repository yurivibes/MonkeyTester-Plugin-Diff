"""ASSERT_LAYOUT action: check where tagged elements sit, independent of screen size.

Ported verbatim (apart from this docstring) from MonkeyTester
`mt-tests/scripts/layout.py`.

Elements come from the controller (`GET /devices/<id>/elements`, device pixels;
MonkeyTester's runner fetches them and sends them with the job) and are
converted to pt/dp with the screen scale, so one test works on every phone.
Tags are Compose `Modifier.testTag` values.

    DIFF: ASSERT_LAYOUT <tag> visible
    DIFF: ASSERT_LAYOUT <tag> inside screen
    DIFF: ASSERT_LAYOUT <tag> inside <tag2>
    DIFF: ASSERT_LAYOUT <tag> centered-x [tol=2%]          (centered-y)
    DIFF: ASSERT_LAYOUT <tag> below <tag2> [gap>=8] [gap<=24]   (above, left-of, right-of)
    DIFF: ASSERT_LAYOUT <tag> no-overlap <tag2>
    DIFF: ASSERT_LAYOUT <tag> aligned-left <tag2> [tol=1]  (-right -top -bottom -center-x -center-y)

Numbers are pt/dp; a trailing % means percent of the screen's width (x checks)
or height (y checks). Default tolerance is 2.
"""
import re

DEFAULT_TOL = 2.0
UNARY = {"visible", "centered-x", "centered-y"}
RELATIONS = {"below", "above", "left-of", "right-of", "inside", "no-overlap",
             "aligned-left", "aligned-right", "aligned-top", "aligned-bottom",
             "aligned-center-x", "aligned-center-y"}
OPTION_RE = re.compile(r"^(tol|gap)(=|>=|<=)(-?\d+(?:\.\d+)?)(%?)$")


class LayoutError(Exception):
    pass


class Box:
    def __init__(self, x, y, w, h, name):
        self.x0, self.y0, self.x1, self.y1, self.name = x, y, x + w, y + h, name

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2

    def __str__(self):
        return (f"{self.name}[x={self.x0:.0f} y={self.y0:.0f} "
                f"w={self.x1 - self.x0:.0f} h={self.y1 - self.y0:.0f}]")


def parse(args):
    """-> (tag, relation, other or None, options list[(key, op, value, is_percent)])."""
    tokens = args.split()
    if len(tokens) < 2:
        raise LayoutError("ASSERT_LAYOUT needs <tag> <check> [...]")
    tag, relation, rest = tokens[0], tokens[1].lower(), tokens[2:]
    other = None
    if relation in RELATIONS:
        if not rest or OPTION_RE.match(rest[0]):
            raise LayoutError(f"'{relation}' needs a second tag (or 'screen' for inside)")
        other, rest = rest[0], rest[1:]
    elif relation not in UNARY:
        raise LayoutError(f"unknown check '{relation}' "
                          f"(use {', '.join(sorted(UNARY | RELATIONS))})")
    options = []
    for token in rest:
        m = OPTION_RE.match(token)
        if not m:
            raise LayoutError(f"bad option {token!r} (tol=N, gap>=N, gap<=N; N in pt/dp or N%)")
        key, op, num, pct = m.groups()
        if key == "tol" and op != "=":
            raise LayoutError("use tol=N")
        if key == "gap" and op == "=":
            raise LayoutError("use gap>=N or gap<=N")
        options.append((key, op, float(num), bool(pct)))
    return tag, relation, other, options


def _boxes(elements, scale, tag):
    return [Box(e["x"] / scale, e["y"] / scale, e["w"] / scale, e["h"] / scale, tag)
            for e in elements if e.get("id") == tag]


def check(args, screen, elements):
    """screen: {width, height, scale} (device px). Returns (passed, message)."""
    tag, relation, other, options = parse(args)
    scale = float(screen["scale"])
    sw, sh = screen["width"] / scale, screen["height"] / scale

    def amount(value, pct, axis):
        return value / 100.0 * (sw if axis == "x" else sh) if pct else value

    axis = "y" if relation in ("below", "above", "centered-y", "aligned-top",
                               "aligned-bottom", "aligned-center-y") else "x"
    tol = DEFAULT_TOL
    gap_min = gap_max = None
    for key, op, value, pct in options:
        v = amount(value, pct, axis)
        if key == "tol":
            tol = v
        elif op == ">=":
            gap_min = v
        else:
            gap_max = v

    found = _boxes(elements, scale, tag)
    if not found:
        ids = sorted({e.get("id") for e in elements if e.get("id")})
        return False, (f"'{tag}' is not on screen (tags on screen: "
                       f"{', '.join(ids[:30]) or 'none'})")
    if len(found) > 1:
        return False, f"'{tag}' matches {len(found)} elements; tags must be unique"
    a = found[0]
    screen_box = Box(0, 0, sw, sh, "screen")

    if relation == "visible":
        ok = a.x1 > 0 and a.y1 > 0 and a.x0 < sw and a.y0 < sh and a.x1 > a.x0 and a.y1 > a.y0
        return ok, f"{a} {'is' if ok else 'is not'} visible on a {sw:.0f}x{sh:.0f} screen"
    if relation == "centered-x":
        off = a.cx - sw / 2
        return abs(off) <= tol, f"{a} center is {off:+.1f} from screen center x (tol {tol:g})"
    if relation == "centered-y":
        off = a.cy - sh / 2
        return abs(off) <= tol, f"{a} center is {off:+.1f} from screen center y (tol {tol:g})"

    if other == "screen":
        if relation != "inside":
            raise LayoutError("'screen' only works with inside")
        b = screen_box
    else:
        others = _boxes(elements, scale, other)
        if not others:
            return False, f"'{other}' is not on screen"
        if len(others) > 1:
            return False, f"'{other}' matches {len(others)} elements; tags must be unique"
        b = others[0]

    if relation == "inside":
        ok = (a.x0 >= b.x0 - tol and a.y0 >= b.y0 - tol
              and a.x1 <= b.x1 + tol and a.y1 <= b.y1 + tol)
        return ok, f"{a} {'is' if ok else 'is not'} inside {b} (tol {tol:g})"
    if relation == "no-overlap":
        ox = min(a.x1, b.x1) - max(a.x0, b.x0)
        oy = min(a.y1, b.y1) - max(a.y0, b.y0)
        ok = ox <= 0 or oy <= 0
        return ok, (f"{a} and {b} " + ("do not overlap" if ok else
                                       f"overlap by {ox:.0f}x{oy:.0f}"))
    if relation.startswith("aligned-"):
        edge = relation[len("aligned-"):]
        pick = {"left": lambda r: r.x0, "right": lambda r: r.x1, "top": lambda r: r.y0,
                "bottom": lambda r: r.y1, "center-x": lambda r: r.cx,
                "center-y": lambda r: r.cy}[edge]
        off = pick(a) - pick(b)
        return abs(off) <= tol, f"{a} {edge} is {off:+.1f} from {b} (tol {tol:g})"

    gap = {"below": a.y0 - b.y1, "above": b.y0 - a.y1,
           "left-of": b.x0 - a.x1, "right-of": a.x0 - b.x1}[relation]
    if gap < -tol:
        return False, f"{a} is not {relation} {b} (gap {gap:.1f})"
    if gap_min is not None and gap < gap_min:
        return False, f"{a} is {relation} {b} but the gap {gap:.1f} < {gap_min:g}"
    if gap_max is not None and gap > gap_max:
        return False, f"{a} is {relation} {b} but the gap {gap:.1f} > {gap_max:g}"
    return True, f"{a} is {relation} {b} (gap {gap:.1f})"
