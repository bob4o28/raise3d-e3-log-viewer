"""Small dependency-free chart widgets for tkinter."""
from __future__ import annotations

import bisect
import math
import tkinter as tk
from datetime import datetime

BG = "#ffffff"
GRID = "#e9ecef"
AXIS = "#868e96"
TEXT = "#343a40"

_TIME_STEPS = [60, 120, 300, 600, 900, 1800, 3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600,
               86400, 2 * 86400, 7 * 86400, 14 * 86400, 30 * 86400]


def _nice_step(span: float, target: int) -> float:
    if span <= 0:
        return 1
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


class TimeChart(tk.Canvas):
    """Multi-series line chart with a time x-axis.

    Mouse wheel = zoom, left-drag = pan, right-drag = zoom to a range,
    double-click = show everything. Hover shows values.
    """

    PAD_L, PAD_R, PAD_T, PAD_B = 56, 16, 14, 34

    def __init__(self, master, **kw):
        super().__init__(master, background=BG, highlightthickness=0, **kw)
        self.t: list[float] = []
        self.series: list[dict] = []
        self.markers: list[tuple[float, str, str]] = []
        self.bands: list[tuple[float, float, str]] = []
        self.x0 = self.x1 = 0.0
        self.y_label = "°C"
        self.on_view_change = None
        self._drag = None
        self._rdrag = None
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda e: self._zoom(e.x, 0.8))
        self.bind("<Button-5>", lambda e: self._zoom(e.x, 1.25))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion_drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.bind("<ButtonPress-3>", self._rpress)
        self.bind("<B3-Motion>", self._rmotion)
        self.bind("<ButtonRelease-3>", self._rrelease)
        self.bind("<Double-Button-1>", lambda e: self.reset_view())
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda e: self.delete("hover"))

    # -- data ---------------------------------------------------------------
    def set_data(self, t, series, markers=(), bands=()):
        self.t = t
        self.series = series
        self.markers = list(markers)
        self.bands = list(bands)
        self.reset_view()

    def reset_view(self):
        if self.t:
            self.x0, self.x1 = self.t[0], self.t[-1]
            if self.x1 <= self.x0:
                self.x1 = self.x0 + 60
        self.redraw()

    def set_view(self, a: float, b: float):
        if b <= a:
            b = a + 60
        self.x0, self.x1 = a, b
        self.redraw()

    # -- geometry -----------------------------------------------------------
    def _plot_box(self):
        w, h = self.winfo_width(), self.winfo_height()
        return self.PAD_L, self.PAD_T, max(w - self.PAD_R, self.PAD_L + 10), max(h - self.PAD_B, self.PAD_T + 10)

    def _tx(self, t):
        l, _, r, _ = self._plot_box()
        return l + (t - self.x0) / (self.x1 - self.x0) * (r - l)

    def _xt(self, x):
        l, _, r, _ = self._plot_box()
        return self.x0 + (x - l) / (r - l) * (self.x1 - self.x0)

    # -- interaction --------------------------------------------------------
    def _wheel(self, e):
        self._zoom(e.x, 0.8 if e.delta > 0 else 1.25)

    def _zoom(self, x, factor):
        if not self.t:
            return
        c = self._xt(x)
        span = (self.x1 - self.x0) * factor
        full = self.t[-1] - self.t[0]
        span = max(60.0, min(span, full * 1.05 if full > 0 else span))
        frac = (c - self.x0) / (self.x1 - self.x0)
        self.x0 = c - frac * span
        self.x1 = self.x0 + span
        self.redraw()

    def _press(self, e):
        self._drag = (e.x, self.x0, self.x1)

    def _motion_drag(self, e):
        if not self._drag:
            return
        x, a, b = self._drag
        l, _, r, _ = self._plot_box()
        dt = (e.x - x) / (r - l) * (b - a)
        self.x0, self.x1 = a - dt, b - dt
        self.redraw()

    def _rpress(self, e):
        self._rdrag = e.x

    def _rmotion(self, e):
        if self._rdrag is None:
            return
        _, t, _, b = self._plot_box()
        self.delete("sel")
        self.create_rectangle(self._rdrag, t, e.x, b, fill="#a5d8ff", outline="#339af0", stipple="gray25", tags="sel")

    def _rrelease(self, e):
        self.delete("sel")
        if self._rdrag is not None and abs(e.x - self._rdrag) > 5:
            a, b = sorted((self._xt(self._rdrag), self._xt(e.x)))
            self.set_view(a, b)
        self._rdrag = None

    def _hover(self, e):
        self.delete("hover")
        if not self.t:
            return
        l, top, r, b = self._plot_box()
        if not (l <= e.x <= r and top <= e.y <= b):
            return
        tt = self._xt(e.x)
        i = bisect.bisect_left(self.t, tt)
        if i >= len(self.t):
            i = len(self.t) - 1
        if i > 0 and abs(self.t[i - 1] - tt) < abs(self.t[i] - tt):
            i -= 1
        if abs(self.t[i] - tt) > max(120, (self.x1 - self.x0) / 200):
            return
        x = self._tx(self.t[i])
        self.create_line(x, top, x, b, fill="#adb5bd", dash=(2, 2), tags="hover")
        lines = [datetime.fromtimestamp(self.t[i]).strftime("%Y-%m-%d %H:%M:%S")]
        for s in self.series:
            if s.get("visible", True):
                lines.append(f"{s['name']}: {s['values'][i]:.1f}")
        text = "\n".join(lines)
        tx = x + 12 if x < (l + r) / 2 else x - 12
        anchor = "nw" if x < (l + r) / 2 else "ne"
        tid = self.create_text(tx, top + 8, text=text, anchor=anchor, font=("Segoe UI", 9), fill=TEXT, tags="hover")
        x1, y1, x2, y2 = self.bbox(tid)
        rid = self.create_rectangle(x1 - 6, y1 - 4, x2 + 6, y2 + 4, fill="#fffbe6", outline="#ced4da", tags="hover")
        self.tag_lower(rid, tid)

    # -- drawing ------------------------------------------------------------
    def redraw(self):
        self.delete("all")
        l, top, r, b = self._plot_box()
        if not self.t:
            self.create_text((l + r) / 2, (top + b) / 2, text="No temperature data in this log bundle",
                             fill=AXIS, font=("Segoe UI", 11))
            return
        i0 = max(bisect.bisect_left(self.t, self.x0) - 1, 0)
        i1 = min(bisect.bisect_right(self.t, self.x1) + 1, len(self.t))
        vis = [s for s in self.series if s.get("visible", True)]
        lo, hi = math.inf, -math.inf
        for s in vis:
            seg = [v for v in s["values"][i0:i1] if -60 < v < 450]
            if seg:
                lo, hi = min(lo, min(seg)), max(hi, max(seg))
        if lo == math.inf:
            lo, hi = 0, 100
        lo = min(lo, 0)
        hi = max(hi + 5, lo + 10)
        self._y0, self._y1 = lo, hi

        def ty(v):
            v = max(lo - (hi - lo) * 0.02, min(v, hi + (hi - lo) * 0.02))
            return b - (v - lo) / (hi - lo) * (b - top)

        # bands (e.g. print jobs)
        for a, z, color in self.bands:
            if z < self.x0 or a > self.x1:
                continue
            self.create_rectangle(max(self._tx(a), l), top, min(self._tx(z), r), b, fill=color, outline="")
        # y grid
        step = _nice_step(hi - lo, 6)
        v = math.ceil(lo / step) * step
        while v <= hi:
            y = ty(v)
            self.create_line(l, y, r, y, fill=GRID)
            self.create_text(l - 6, y, text=f"{v:g}", anchor="e", fill=AXIS, font=("Segoe UI", 8))
            v += step
        self.create_text(8, top, text=self.y_label, anchor="nw", fill=AXIS, font=("Segoe UI", 8))
        # x grid
        span = self.x1 - self.x0
        width = r - l
        tstep = next((s for s in _TIME_STEPS if span / s <= max(width / 110, 2)), _TIME_STEPS[-1])
        first = datetime.fromtimestamp(self.x0)
        # align to local midnight
        mid = datetime(first.year, first.month, first.day).timestamp()
        tt = mid + math.ceil((self.x0 - mid) / tstep) * tstep
        last_day = None
        while tt <= self.x1:
            x = self._tx(tt)
            d = datetime.fromtimestamp(tt)
            self.create_line(x, top, x, b, fill=GRID)
            lab = d.strftime("%H:%M") if tstep < 86400 else d.strftime("%d %b")
            self.create_text(x, b + 4, text=lab, anchor="n", fill=AXIS, font=("Segoe UI", 8))
            day = d.date()
            if tstep < 86400 and day != last_day:
                self.create_text(x, b + 17, text=d.strftime("%a %d %b"), anchor="n", fill=TEXT, font=("Segoe UI", 8, "bold"))
                last_day = day
            tt += tstep
        self.create_rectangle(l, top, r, b, outline="#ced4da")
        # series
        n = i1 - i0
        bucket = max(1, int(n / max(width, 1)))
        for s in vis:
            vals = s["values"]
            pts: list[float] = []

            def flush():
                if len(pts) >= 4:
                    self.create_line(*pts, fill=s["color"], width=s.get("width", 1.5),
                                     dash=s.get("dash") or "")
                pts.clear()

            prev_t = None
            for k in range(i0, i1, bucket):
                chunk_t = self.t[k]
                if prev_t is not None and chunk_t - prev_t > 90 * bucket:
                    flush()
                prev_t = chunk_t
                x = self._tx(chunk_t)
                if bucket == 1:
                    pts += [x, ty(vals[k])]
                else:
                    seg = vals[k:k + bucket]
                    mn, mx = min(seg), max(seg)
                    pts += [x, ty(mn), x + 0.5, ty(mx)]
            flush()
        # markers
        last_label_x = -1e9
        for mt, color, label in sorted(self.markers):
            if self.x0 <= mt <= self.x1:
                x = self._tx(mt)
                self.create_line(x, top, x, b, fill=color, width=2)
                if x - last_label_x > 70 and x < r - 40:
                    self.create_text(x + 3, top + 2, text=label, anchor="nw", fill=color, font=("Segoe UI", 8, "bold"))
                    last_label_x = x
        if self.on_view_change:
            self.on_view_change(self.x0, self.x1)


class BarChart(tk.Canvas):
    """Simple vertical bar chart: list of (label, value, color)."""

    def __init__(self, master, title="", unit="", **kw):
        super().__init__(master, background=BG, highlightthickness=0, **kw)
        self.title = title
        self.unit = unit
        self.items: list[tuple[str, float, str]] = []
        self.bind("<Configure>", lambda e: self.redraw())

    def set_items(self, items):
        self.items = items
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        l, r, top, b = 44, w - 10, 26, h - 40
        self.create_text(10, 6, text=self.title, anchor="nw", fill=TEXT, font=("Segoe UI", 9, "bold"))
        if not self.items or r - l < 20:
            return
        vmax = max(v for _, v, _ in self.items) or 1
        step = _nice_step(vmax, 4)
        top_v = math.ceil(vmax / step) * step
        v = 0.0
        while v <= top_v + 1e-9:
            y = b - v / top_v * (b - top)
            self.create_line(l, y, r, y, fill=GRID)
            self.create_text(l - 5, y, text=f"{v:g}", anchor="e", fill=AXIS, font=("Segoe UI", 8))
            v += step
        n = len(self.items)
        bw = (r - l) / n
        every = max(1, int(28 / bw) + 1) if bw < 28 else 1
        for i, (lab, val, color) in enumerate(self.items):
            x0 = l + i * bw + bw * 0.15
            x1 = l + (i + 1) * bw - bw * 0.15
            y = b - val / top_v * (b - top)
            if val > 0:
                self.create_rectangle(x0, y, x1, b, fill=color, outline="")
            if i % every == 0:
                self.create_text((x0 + x1) / 2, b + 4, text=lab, anchor="n", fill=AXIS, font=("Segoe UI", 7))
        self.create_text(r, 6, text=self.unit, anchor="ne", fill=AXIS, font=("Segoe UI", 8))


def diverging_color(v: float, vmax: float) -> str:
    """Blue (low) - white - red (high)."""
    if vmax <= 0:
        return "#ffffff"
    f = max(-1.0, min(1.0, v / vmax))
    if f >= 0:
        c0, c1 = (255, 255, 255), (201, 42, 42)
    else:
        f = -f
        c0, c1 = (255, 255, 255), (25, 113, 194)
    rgb = [int(a + (b - a) * f) for a, b in zip(c0, c1)]
    return "#%02x%02x%02x" % tuple(rgb)
