"""Raise3D E3 Log Viewer - tkinter GUI."""
from __future__ import annotations

import csv
import glob
import os
import queue
import re
import sys
import tempfile
import threading
import tkinter as tk
from collections import defaultdict
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

from . import parser as P
from .charts import BarChart, TimeChart, diverging_color

APP_TITLE = "Raise3D E3 Log Viewer"
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_H = ("Segoe UI", 13, "bold")
MONO = ("Consolas", 10)

SEV_COLORS = {"critical": "#c92a2a", "error": "#c92a2a", "warning": "#e67700", "info": "#1864ab"}
SEV_BG = {"critical": "#fff5f5", "error": "#fff5f5", "warning": "#fff9db", "info": "#ffffff"}
RESULT_BG = {"Completed": "#ebfbee", "Stopped": "#fff4e6", "Paused": "#fff9db", "No end record": "#f1f3f5"}

SERIES_STYLE = {
    "T0": ("Left nozzle", "#e8590c", None, 1.6),
    "T0_target": ("Left nozzle target", "#ffa94d", (4, 3), 1),
    "T1": ("Right nozzle", "#1c7ed6", None, 1.6),
    "T1_target": ("Right nozzle target", "#74c0fc", (4, 3), 1),
    "B": ("Bed", "#c2255c", None, 1.8),
    "B_target": ("Bed target", "#f783ac", (4, 3), 1),
}


def fmt_dt(d: datetime | None, sec=False) -> str:
    if not d:
        return "–"
    return d.strftime("%Y-%m-%d %H:%M:%S" if sec else "%Y-%m-%d %H:%M")


def fmt_dur(s: float | None) -> str:
    if not s:
        return "–"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {sec:02d}s"


def make_tree(parent, columns, widths, anchors=None, height=10, show="headings"):
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c for c in columns], show=show, height=height, selectmode="browse")
    vs = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vs.grid(row=0, column=1, sticky="ns")
    hs.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    for i, c in enumerate(columns):
        tree.heading(c, text=c, command=lambda c=c: sort_tree(tree, c, False))
        anchor = (anchors or {}).get(c, "w")
        tree.column(c, width=widths[i], minwidth=40, anchor=anchor, stretch=(i == len(columns) - 1 or widths[i] >= 250))
    return frame, tree


def _sort_key(v: str):
    v2 = v.replace("%", "").replace(",", "").strip()
    m = re.fullmatch(r"(\d+)h (\d+)m", v2)
    if m:
        return (0, int(m.group(1)) * 60 + int(m.group(2)), "")
    m = re.fullmatch(r"(\d+)m (\d+)s", v2)
    if m:
        return (0, int(m.group(1)) + int(m.group(2)) / 60, "")
    try:
        return (0, float(v2), "")
    except ValueError:
        return (1, 0, v.lower())


def sort_tree(tree: ttk.Treeview, col: str, reverse: bool):
    items = [(tree.set(k, col), k) for k in tree.get_children("")]
    items.sort(key=lambda x: _sort_key(x[0]), reverse=reverse)
    for i, (_, k) in enumerate(items):
        tree.move(k, "", i)
    tree.heading(col, command=lambda: sort_tree(tree, col, not reverse))


class ReadOnlyText(tk.Text):
    def __init__(self, master, **kw):
        kw.setdefault("wrap", "word")
        kw.setdefault("font", FONT)
        kw.setdefault("relief", "flat")
        kw.setdefault("padx", 10)
        kw.setdefault("pady", 8)
        super().__init__(master, **kw)
        self.configure(state="disabled")

    def set(self, chunks):
        """chunks: list of (text, tag-or-None)."""
        self.configure(state="normal")
        self.delete("1.0", "end")
        for text, tag in chunks:
            self.insert("end", text, tag or ())
        self.configure(state="disabled")


# ==========================================================================
class App(tk.Tk):
    def __init__(self, initial: str | None = None):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1360x860")
        self.minsize(980, 640)
        self.data: P.LogData | None = None
        self._q: queue.Queue = queue.Queue()
        self._tmpdir = tempfile.mkdtemp(prefix="r3dlog_")
        self._style()
        self._menu()
        self._layout()
        self.after(100, self._poll)
        if initial:
            self.after(200, lambda: self.open_path(initial))
        else:
            self.after(200, self._auto_open)

    # -- look -----------------------------------------------------------------
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        self.option_add("*Font", FONT)
        st.configure(".", font=FONT, background="#f8f9fa")
        st.configure("TFrame", background="#f8f9fa")
        st.configure("Card.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        st.configure("TLabel", background="#f8f9fa")
        st.configure("Card.TLabel", background="#ffffff")
        st.configure("CardTitle.TLabel", background="#ffffff", foreground="#868e96", font=("Segoe UI", 9))
        st.configure("CardValue.TLabel", background="#ffffff", foreground="#212529", font=("Segoe UI", 14, "bold"))
        st.configure("H.TLabel", font=FONT_H)
        st.configure("Muted.TLabel", foreground="#868e96")
        st.configure("TNotebook.Tab", padding=(14, 6), font=FONT)
        st.configure("Treeview", rowheight=24, font=FONT, background="#ffffff", fieldbackground="#ffffff")
        st.configure("Treeview.Heading", font=FONT_B, padding=(4, 4))
        st.map("Treeview", background=[("selected", "#4dabf7")], foreground=[("selected", "#ffffff")])
        st.configure("Accent.TButton", font=FONT_B)
        self.configure(background="#f8f9fa")

    def _menu(self):
        m = tk.Menu(self)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="Open log archive (.tar.gz)…", command=self.ask_open_file, accelerator="Ctrl+O")
        fm.add_command(label="Open extracted folder…", command=self.ask_open_dir)
        fm.add_separator()
        fm.add_command(label="Export print history to CSV…", command=self.export_csv)
        fm.add_command(label="Export findings report (.txt)…", command=self.export_report)
        fm.add_separator()
        fm.add_command(label="Exit", command=self.destroy)
        m.add_cascade(label="File", menu=fm)
        hm = tk.Menu(m, tearoff=0)
        hm.add_command(label="How to use", command=self.show_help)
        hm.add_command(label="About", command=lambda: messagebox.showinfo(
            APP_TITLE, f"{APP_TITLE}\n\nVisualises the diagnostic log export of a Raise3D E3 printer "
                       "(Settings → Machine → Export logs on the printer).\n\n"
                       "Independent project – not affiliated with, endorsed by, or supported by Raise3D "
                       "Technologies. \"Raise3D\", \"RaiseTouch\" and \"E3\" are trademarks of their respective "
                       "owner, used only to describe compatibility.\n\n"
                       "Automatic findings are best guesses from the log data, not an official diagnosis. "
                       "Have safety-related problems checked by a qualified technician or Raise3D support.\n\n"
                       "MIT License – provided \"as is\", without warranty."))
        m.add_cascade(label="Help", menu=hm)
        self.config(menu=m)
        self.bind("<Control-o>", lambda e: self.ask_open_file())

    def _layout(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Button(top, text="📂  Open log archive…", style="Accent.TButton", command=self.ask_open_file).pack(side="left")
        ttk.Button(top, text="Open folder…", command=self.ask_open_dir).pack(side="left", padx=6)
        self.lbl_source = ttk.Label(top, text="No logs loaded", style="Muted.TLabel")
        self.lbl_source.pack(side="left", padx=12)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.tabs: dict[str, ttk.Frame] = {}
        for name in ["Overview", "Print history", "Temperatures", "Timeline", "Bed leveling", "Log files", "Settings"]:
            f = ttk.Frame(self.nb, padding=8)
            self.nb.add(f, text=name)
            self.tabs[name] = f
        self._build_overview()
        self._build_history()
        self._build_temps()
        self._build_timeline()
        self._build_leveling()
        self._build_files()
        self._build_settings()

        self.status = ttk.Label(self, text="Open the .tar.gz log archive exported from the printer.", anchor="w",
                                padding=(10, 3), style="Muted.TLabel")
        self.status.pack(fill="x")

    # -- loading ---------------------------------------------------------------
    def _auto_open(self):
        here = os.path.dirname(os.path.abspath(sys.argv[0]))
        cands = []
        for d in (here, os.path.dirname(here), os.getcwd()):
            cands += glob.glob(os.path.join(d, "all-raisetouch-*.tar.gz"))
        cands = sorted(set(cands), key=os.path.getmtime)
        if cands:
            self.open_path(cands[-1])

    def ask_open_file(self):
        p = filedialog.askopenfilename(title="Open Raise3D log archive",
                                       filetypes=[("Raise3D log archive", "*.tar.gz *.tgz *.tar"), ("All files", "*.*")])
        if p:
            self.open_path(p)

    def ask_open_dir(self):
        p = filedialog.askdirectory(title="Open folder with extracted Raise3D logs")
        if p:
            self.open_path(p)

    def open_path(self, path: str):
        self.status.config(text=f"Loading {path} …")
        self.config(cursor="watch")

        def work():
            try:
                d = P.load(path, progress=lambda m: self._q.put(("progress", m)))
                self._q.put(("done", d))
            except Exception as ex:  # noqa: BLE001
                import traceback
                self._q.put(("error", f"{ex}\n\n{traceback.format_exc()}"))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, val = self._q.get_nowait()
                if kind == "progress":
                    self.status.config(text=val)
                elif kind == "done":
                    self.config(cursor="")
                    self.show(val)
                elif kind == "error":
                    self.config(cursor="")
                    self.status.config(text="Failed to load logs.")
                    messagebox.showerror(APP_TITLE, "Could not read the logs:\n\n" + val[:1500])
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def show(self, d: P.LogData):
        self.data = d
        self.title(f"{APP_TITLE} – {d.info.get('printer_type', '')} #{d.info.get('serial_no', '')}")
        self.lbl_source.config(text=os.path.basename(d.bundle.source))
        self._fill_overview()
        self._fill_history()
        self._fill_temps()
        self._fill_timeline()
        self._fill_leveling()
        self._fill_files()
        self._fill_settings()
        crit = sum(1 for f in d.findings if f.severity == "critical")
        self.status.config(text=f"Loaded {len(d.bundle.files)} files · {len(d.jobs)} print jobs · "
                                f"{len(d.temps.t):,} temperature samples · {len(d.events)} events · "
                                f"{crit} critical finding(s)")
        self.nb.select(self.tabs["Overview"])

    # ======================================================================
    # Overview
    # ======================================================================
    def _build_overview(self):
        f = self.tabs["Overview"]
        self.ov_cards = ttk.Frame(f)
        self.ov_cards.pack(fill="x")
        pw = ttk.PanedWindow(f, orient="horizontal")
        pw.pack(fill="both", expand=True, pady=(10, 0))
        left = ttk.Frame(pw)
        ttk.Label(left, text="Findings – automatic analysis", style="H.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Label(left, text="Double-click a finding to jump to the related data.", style="Muted.TLabel").pack(anchor="w")
        fr, self.ov_tree = make_tree(left, ["Severity", "Finding", "When"], [90, 470, 140], height=12)
        fr.pack(fill="both", expand=True, pady=4)
        for sev, col in SEV_COLORS.items():
            self.ov_tree.tag_configure(sev, foreground=col, background=SEV_BG[sev])
        self.ov_detail = ReadOnlyText(left, height=7, background="#ffffff")
        self.ov_detail.pack(fill="x")
        self.ov_tree.bind("<<TreeviewSelect>>", self._ov_select)
        self.ov_tree.bind("<Double-1>", self._ov_jump)
        pw.add(left, weight=3)
        right = ttk.Frame(pw)
        ttk.Label(right, text="Printer information", style="H.TLabel").pack(anchor="w", pady=(0, 4))
        self.ov_info = ReadOnlyText(right, background="#ffffff", width=46)
        self.ov_info.pack(fill="both", expand=True)
        self.ov_info.tag_configure("k", foreground="#868e96")
        self.ov_info.tag_configure("h", font=FONT_B, spacing1=8)
        pw.add(right, weight=2)
        self._ov_findings: list[P.Finding] = []

    def _card(self, parent, title, value, col, color="#212529"):
        c = ttk.Frame(parent, style="Card.TFrame", padding=(12, 8))
        c.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
        parent.columnconfigure(col, weight=1)
        ttk.Label(c, text=title, style="CardTitle.TLabel").pack(anchor="w")
        lbl = ttk.Label(c, text=value, style="CardValue.TLabel")
        lbl.configure(foreground=color)
        lbl.pack(anchor="w")

    def _fill_overview(self):
        d = self.data
        for w in self.ov_cards.winfo_children():
            w.destroy()
        real = [j for j in d.jobs if not j.is_calibration]
        comp = [j for j in real if j.result == "Completed"]
        hours = sum(j.elapsed_s for j in d.jobs) / 3600
        fil = sum(sum(j.used_m) for j in d.jobs)
        crit = sum(1 for x in d.findings if x.severity == "critical")
        warn = sum(1 for x in d.findings if x.severity == "warning")
        cards = [
            ("Print jobs in history", f"{len(real)}", "#212529"),
            ("Completed", f"{len(comp)} ({(100 * len(comp) / len(real)) if real else 0:.0f}%)", "#2b8a3e"),
            ("Printing time", f"{hours:.1f} h", "#212529"),
            ("Filament used", f"{fil:.0f} m", "#212529"),
            ("Critical findings", str(crit), "#c92a2a" if crit else "#2b8a3e"),
            ("Warnings", str(warn), "#e67700" if warn else "#2b8a3e"),
        ]
        for i, (t, v, c) in enumerate(cards):
            self._card(self.ov_cards, t, v, i, c)

        self.ov_tree.delete(*self.ov_tree.get_children())
        self._ov_findings = d.findings
        icons = {"critical": "⛔ Critical", "warning": "⚠ Warning", "info": "ℹ Info"}
        for i, x in enumerate(d.findings):
            self.ov_tree.insert("", "end", iid=str(i), values=(icons.get(x.severity, x.severity), x.title, fmt_dt(x.time)),
                                tags=(x.severity,))
        if not d.findings:
            self.ov_detail.set([("No problems were detected automatically.", None)])
        else:
            self.ov_tree.selection_set("0")

        cfg = d.configs
        pc = cfg.get("printer.cfg", {})
        ex = cfg.get("extruders.cfg", {})
        ts = cfg.get("timestat.cfg", {})
        ps = cfg.get("print-stat.cfg", {})
        e2 = cfg.get("e2config.cfg", {})

        def kv(k, v):
            return [(f"{k}: ", "k"), (f"{v}\n", None)]

        ch = [("Machine\n", "h")]
        ch += kv("Model", d.info.get("printer_type", "–"))
        ch += kv("Name", d.info.get("printer_name", "–"))
        ch += kv("Serial number", d.info.get("serial_no", "–"))
        ch += kv("RaiseTouch (screen) version", d.info.get("raisetouch_version", "–"))
        ch += kv("Firmware (Klipper) version", d.info.get("firmware_version", "–"))
        ch += kv("Motion board", d.info.get("mcb_version", "–"))
        ch += kv("Log export time", fmt_dt(d.bundle.export_time, True))
        ch += [("Lifetime counters\n", "h")]
        try:
            ch += kv("Total power-on time", f"{int(ts.get('total_poweron_time', 0)) / 3600:.0f} h")
        except ValueError:
            pass
        if ps:
            ch += kv("Prints started / completed (printer counter)",
                     f"{ps.get('total_print_files', '–')} / {ps.get('total_completed_files', '–')}")
        if ex:
            ch += kv("Filament total – left", f"{ex.get('total_filament_used1_mm', 0) / 1000:.0f} m  "
                                              f"(≈ {ex.get('total_filament_used1_mg', 0) / 1e6:.2f} kg)")
            ch += kv("Filament total – right", f"{ex.get('total_filament_used2_mm', 0) / 1000:.0f} m  "
                                               f"(≈ {ex.get('total_filament_used2_mg', 0) / 1e6:.2f} kg)")
        ch += [("Hardware setup\n", "h")]
        ch += kv("Nozzles (L / R)", f"{pc.get('left_nozzle_diameter', 0):.2f} / {pc.get('right_nozzle_diameter', 0):.2f} mm")
        ch += kv("Build volume", f"{e2.get('x_max_travel', '?')} × {e2.get('y_max_travel', '?')} × {e2.get('z_max_travel', '?')} mm")
        ch += kv("Filament run-out sensor", "on" if pc.get("filament_sensor_open") else "off")
        ch += kv("Door sensor", "on" if e2.get("enable_open_door_sensor") else "off")
        ch += kv("Automatic power saving", "on" if e2.get("enable_automatic_power_saving") else "off")
        ch += [("Data coverage\n", "h")]
        if d.jobs:
            ch += kv("Print history", f"{fmt_dt(d.jobs[0].start)} → {fmt_dt(d.jobs[-1].start)}")
        if d.temps.t:
            a, b = datetime.fromtimestamp(d.temps.t[0]), datetime.fromtimestamp(d.temps.t[-1])
            ch += kv("Temperature log", f"{fmt_dt(a)} → {fmt_dt(b)} ({(b - a).total_seconds() / 3600:.0f} h)")
        if d.leveling:
            ch += kv("Bed leveling runs", f"{len(d.leveling)}")
        ch += [("\nNote: the printer only keeps the most recent ~1 day of temperature data and its recent print "
                "history; older data is overwritten.", "k")]
        self.ov_info.set(ch)

    def _ov_select(self, _e=None):
        sel = self.ov_tree.selection()
        if not sel:
            return
        x = self._ov_findings[int(sel[0])]
        when = ""
        if x.time:
            when = fmt_dt(x.time, True) + (f"  →  {fmt_dt(x.time_end, True)}" if x.time_end and x.time_end != x.time else "")
        self.ov_detail.tag_configure("t", font=FONT_B, foreground=SEV_COLORS.get(x.severity, "#000"))
        self.ov_detail.tag_configure("m", foreground="#868e96")
        self.ov_detail.set([(x.title + "\n", "t"), ((when + "\n") if when else "", "m"), (x.detail, None)])

    def _ov_jump(self, _e=None):
        sel = self.ov_tree.selection()
        if not sel:
            return
        x = self._ov_findings[int(sel[0])]
        if x.tab == "Temperatures" and x.time:
            a = x.time - timedelta(minutes=30)
            b = (x.time_end or x.time) + timedelta(minutes=30)
            self.temp_chart.set_view(a.timestamp(), b.timestamp())
        elif x.tab == "Timeline" and x.time:
            self._tl_goto(x.time)
        if x.tab in self.tabs:
            self.nb.select(self.tabs[x.tab])

    # ======================================================================
    # Print history
    # ======================================================================
    COLS_H = ["#", "Start", "File", "Result", "Duration", "Slicer estimate", "Progress", "Extruder",
              "Filament (m)", "Est. weight (g)", "Pauses"]

    def _build_history(self):
        f = self.tabs["Print history"]
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Label(bar, text="Search:").pack(side="left")
        self.h_search = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.h_search, width=28)
        e.pack(side="left", padx=4)
        self.h_search.trace_add("write", lambda *_: self._fill_history_rows())
        ttk.Label(bar, text="Result:").pack(side="left", padx=(12, 0))
        self.h_result = ttk.Combobox(bar, values=["All", "Completed", "Stopped", "No end record"], state="readonly", width=14)
        self.h_result.set("All")
        self.h_result.pack(side="left", padx=4)
        self.h_result.bind("<<ComboboxSelected>>", lambda e: self._fill_history_rows())
        self.h_hidecal = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Hide calibration prints", variable=self.h_hidecal,
                        command=self._fill_history_rows).pack(side="left", padx=12)
        ttk.Button(bar, text="Export CSV…", command=self.export_csv).pack(side="right")
        self.h_stats = ttk.Label(f, text="", style="Muted.TLabel")
        self.h_stats.pack(fill="x", pady=(6, 2))

        pw = ttk.PanedWindow(f, orient="vertical")
        pw.pack(fill="both", expand=True)
        widths = [40, 130, 330, 110, 80, 105, 70, 70, 90, 105, 60]
        anchors = {c: "e" for c in ["#", "Duration", "Slicer estimate", "Progress", "Filament (m)", "Est. weight (g)", "Pauses"]}
        fr, self.h_tree = make_tree(pw, self.COLS_H, widths, anchors, height=14)
        for res, bg in RESULT_BG.items():
            self.h_tree.tag_configure(res, background=bg)
        self.h_tree.bind("<<TreeviewSelect>>", self._h_select)
        pw.add(fr, weight=3)
        bottom = ttk.PanedWindow(pw, orient="horizontal")
        self.h_detail = ReadOnlyText(bottom, background="#ffffff", font=FONT, height=10)
        self.h_detail.tag_configure("h", font=FONT_B)
        self.h_detail.tag_configure("k", foreground="#868e96")
        bottom.add(self.h_detail, weight=1)
        self.h_chart = BarChart(bottom, title="Printing hours per day", unit="hours", height=200)
        bottom.add(self.h_chart, weight=1)
        pw.add(bottom, weight=2)

    def _fill_history(self):
        self._fill_history_rows()
        d = self.data
        per_day: dict = defaultdict(float)
        for j in d.jobs:
            if j.start:
                per_day[j.start.date()] += j.elapsed_s / 3600
        if per_day:
            day0, day1 = min(per_day), max(per_day)
            items = []
            cur = day0
            while cur <= day1:
                items.append((cur.strftime("%d.%m"), per_day.get(cur, 0.0), "#4dabf7"))
                cur += timedelta(days=1)
            self.h_chart.set_items(items)
        else:
            self.h_chart.set_items([])

    def _filtered_jobs(self):
        if not self.data:
            return []
        q = self.h_search.get().lower().strip()
        res = self.h_result.get()
        out = []
        for i, j in enumerate(self.data.jobs, 1):
            if self.h_hidecal.get() and j.is_calibration:
                continue
            if res != "All" and j.result != res:
                continue
            if q and q not in j.file_name.lower():
                continue
            out.append((i, j))
        return out

    def _fill_history_rows(self):
        t = self.h_tree
        t.delete(*t.get_children())
        jobs = self._filtered_jobs()
        for i, j in jobs:
            lm, rm = j.used_m
            gl, gr = j.est_g
            prog = j.progress
            t.insert("", "end", iid=str(i - 1), tags=(j.result,), values=(
                i, fmt_dt(j.start), j.file_name, j.result, fmt_dur(j.elapsed_s), fmt_dur(j.est_s),
                (f"{prog:.0f}%" if j.result == "Completed" or any(e.lines_done for e in j.events) else f"~{prog:.0f}%")
                if prog is not None and j.result != "No end record" else "–", j.extruders,
                f"{lm + rm:.2f}" if lm + rm else "–", f"{gl + gr:.1f}" if gl + gr else "–", j.pauses or ""))
        n = len(jobs)
        comp = sum(1 for _, j in jobs if j.result == "Completed")
        stop = sum(1 for _, j in jobs if j.result == "Stopped")
        hrs = sum(j.elapsed_s for _, j in jobs) / 3600
        fil = sum(sum(j.used_m) for _, j in jobs)
        grams = sum(sum(j.est_g) for _, j in jobs if j.result == "Completed")
        self.h_stats.config(text=f"{n} jobs shown · {comp} completed · {stop} stopped · {hrs:.1f} h printing · "
                                 f"{fil:.1f} m filament used · ≈{grams:.0f} g in completed prints (slicer estimate)")

    def _h_select(self, _e=None):
        sel = self.h_tree.selection()
        if not sel:
            return
        j = self.data.jobs[int(sel[0])]
        lm, rm = j.used_m
        gl, gr = j.est_g
        ch = [(j.file_name + "\n", "h")]

        def kv(k, v):
            return [(f"{k}: ", "k"), (f"{v}\n", None)]
        ch += kv("Result", j.result)
        ch += kv("Started", fmt_dt(j.start, True))
        ch += kv("Ended", fmt_dt(j.end, True))
        ch += kv("Printing time", f"{fmt_dur(j.elapsed_s)} (slicer estimate {fmt_dur(j.est_s)})")
        ch += kv("Filament used", f"left {lm:.2f} m, right {rm:.2f} m")
        ch += kv("Planned weight (slicer)", f"left {gl:.1f} g, right {gr:.1f} g")
        size = max((e.file_size for e in j.events), default=0)
        ch += kv("G-code size", f"{size / 1e6:.1f} MB")
        ch += kv("File path on printer", j.file_path)
        ch += [("\nEvent log\n", "h")]
        for e in j.events:
            ch += [(f"  {fmt_dt(e.time, True)}  ", "k"), (f"{e.name}\n", None)]
        self.h_detail.set(ch)

    def export_csv(self):
        if not self.data:
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                         initialfile="print_history.csv")
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["start", "end", "file", "result", "duration_min", "estimate_min", "progress_pct",
                        "extruder", "filament_left_m", "filament_right_m", "est_weight_left_g",
                        "est_weight_right_g", "pauses", "path"])
            for _, j in self._filtered_jobs():
                lm, rm = j.used_m
                gl, gr = j.est_g
                w.writerow([fmt_dt(j.start, True), fmt_dt(j.end, True), j.file_name, j.result,
                            round(j.elapsed_s / 60, 1), round(j.est_s / 60, 1),
                            "" if j.progress is None else round(j.progress), j.extruders,
                            round(lm, 3), round(rm, 3), round(gl, 1), round(gr, 1), j.pauses, j.file_path])
        self.status.config(text=f"Exported print history to {p}")

    # ======================================================================
    # Temperatures
    # ======================================================================
    def _build_temps(self):
        f = self.tabs["Temperatures"]
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self.temp_vars: dict[str, tk.BooleanVar] = {}
        for key, (name, color, dash, _w) in SERIES_STYLE.items():
            v = tk.BooleanVar(value=True)
            self.temp_vars[key] = v
            cb = tk.Checkbutton(bar, text=name, variable=v, fg=color, selectcolor="#ffffff", bg="#f8f9fa",
                                activebackground="#f8f9fa", font=FONT_B if dash is None else FONT,
                                command=self._temps_visibility)
            cb.pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Show all", command=lambda: self.temp_chart.reset_view()).pack(side="right")
        for label, hours in (("Last 1 h", 1), ("Last 6 h", 6)):
            ttk.Button(bar, text=label, command=lambda h=hours: self._temps_last(h)).pack(side="right", padx=3)
        self.temp_chart = TimeChart(f)
        self.temp_chart.pack(fill="both", expand=True, pady=6)
        self.temp_info = ttk.Label(f, style="Muted.TLabel", text=(
            "Mouse wheel = zoom · drag = pan · right-drag = zoom to a range · double-click = show all · "
            "shaded areas = print jobs · red lines = firmware shutdowns"))
        self.temp_info.pack(fill="x")
        self.temp_range = ttk.Label(f, text="")
        self.temp_range.pack(fill="x")
        self.temp_chart.on_view_change = self._temps_view_changed

    def _fill_temps(self):
        d = self.data
        ts = d.temps
        series = []
        for key, (name, color, dash, w) in SERIES_STYLE.items():
            series.append(dict(name=name, values=ts.channels.get(key, []), color=color, dash=dash, width=w,
                               visible=self.temp_vars[key].get(), key=key))
        markers = []
        seen = []
        for e in d.events:
            if e.category == "Shutdown" and not any(abs((e.time - s).total_seconds()) < 60 for s in seen):
                seen.append(e.time)
                markers.append((e.time.timestamp(), "#e03131", "SHUTDOWN"))
            elif e.category == "Startup" and e.source == "serial":
                markers.append((e.time.timestamp(), "#868e96", "restart"))
        bands = []
        for j in d.jobs:
            if j.start:
                end = j.end or (j.start + timedelta(seconds=max(j.elapsed_s, 60)))
                bands.append((j.start.timestamp(), end.timestamp(), "#e7f5ff"))
        self.temp_chart.set_data(ts.t, series, markers, bands)

    def _temps_visibility(self):
        for s in self.temp_chart.series:
            s["visible"] = self.temp_vars[s["key"]].get()
        self.temp_chart.redraw()

    def _temps_last(self, hours):
        if self.temp_chart.t:
            end = self.temp_chart.t[-1]
            self.temp_chart.set_view(end - hours * 3600, end)

    def _temps_view_changed(self, a, b):
        if not self.data:
            return
        self.temp_range.config(text=f"Showing {fmt_dt(datetime.fromtimestamp(a), True)}  →  "
                                    f"{fmt_dt(datetime.fromtimestamp(b), True)}   ({fmt_dur(b - a)})")

    # ======================================================================
    # Timeline
    # ======================================================================
    def _build_timeline(self):
        f = self.tabs["Timeline"]
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Label(bar, text="Search:").pack(side="left")
        self.tl_search = tk.StringVar()
        ttk.Entry(bar, textvariable=self.tl_search, width=30).pack(side="left", padx=4)
        self.tl_search.trace_add("write", lambda *_: self._fill_timeline_rows())
        ttk.Label(bar, text="Show:").pack(side="left", padx=(12, 2))
        self.tl_sev = ttk.Combobox(bar, values=["Everything", "Warnings and errors", "Errors only"], state="readonly", width=20)
        self.tl_sev.set("Everything")
        self.tl_sev.pack(side="left")
        self.tl_sev.bind("<<ComboboxSelected>>", lambda e: self._fill_timeline_rows())
        self.tl_cats_frame = ttk.Frame(f)
        self.tl_cats_frame.pack(fill="x", pady=4)
        self.tl_cat_vars: dict[str, tk.BooleanVar] = {}
        fr, self.tl_tree = make_tree(f, ["Time", "Category", "Source", "Message"], [150, 110, 110, 800], height=20)
        fr.pack(fill="both", expand=True)
        for sev, col in SEV_COLORS.items():
            self.tl_tree.tag_configure(sev, foreground=col, background=SEV_BG[sev])
        self.tl_tree.bind("<Double-1>", self._tl_open)
        ttk.Label(f, text="Double-click an event to open it in the original log file.", style="Muted.TLabel").pack(anchor="w")
        self._tl_events: list[P.LogEvent] = []

    def _fill_timeline(self):
        for w in self.tl_cats_frame.winfo_children():
            w.destroy()
        cats = sorted({e.category for e in self.data.events})
        ttk.Label(self.tl_cats_frame, text="Categories:").pack(side="left")
        noisy = {"Filament", "Machine state", "Power"}
        self.tl_cat_vars = {}
        for c in cats:
            v = tk.BooleanVar(value=c not in noisy)
            self.tl_cat_vars[c] = v
            ttk.Checkbutton(self.tl_cats_frame, text=c, variable=v, command=self._fill_timeline_rows).pack(side="left", padx=3)
        self._fill_timeline_rows()

    def _fill_timeline_rows(self):
        if not self.data:
            return
        t = self.tl_tree
        t.delete(*t.get_children())
        q = self.tl_search.get().lower().strip()
        mode = self.tl_sev.get()
        self._tl_events = []
        for e in self.data.events:
            if not self.tl_cat_vars.get(e.category, tk.BooleanVar(value=True)).get():
                continue
            if mode == "Warnings and errors" and e.severity == "info":
                continue
            if mode == "Errors only" and e.severity != "error":
                continue
            if q and q not in e.message.lower() and q not in e.category.lower():
                continue
            self._tl_events.append(e)
            t.insert("", "end", iid=str(len(self._tl_events) - 1), tags=(e.severity,),
                     values=(fmt_dt(e.time, True), e.category, e.source, e.message))

    def _tl_goto(self, when: datetime):
        self.tl_search.set("")
        for v in self.tl_cat_vars.values():
            v.set(True)
        self._fill_timeline_rows()
        best = None
        for i, e in enumerate(self._tl_events):
            if e.time >= when - timedelta(seconds=1):
                best = i
                break
        if best is not None:
            self.tl_tree.selection_set(str(best))
            self.tl_tree.see(str(best))

    def _tl_open(self, _e=None):
        sel = self.tl_tree.selection()
        if not sel:
            return
        e = self._tl_events[int(sel[0])]
        if e.file:
            self.open_log_file(e.file, e.line)

    # ======================================================================
    # Bed leveling
    # ======================================================================
    def _build_leveling(self):
        f = self.tabs["Bed leveling"]
        pw = ttk.PanedWindow(f, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw)
        fr, self.lv_tree = make_tree(left, ["Time", "Area", "Points", "Flatness", "RMS"], [140, 80, 55, 75, 65],
                                     {"Points": "e", "Flatness": "e", "RMS": "e"}, height=20)
        fr.pack(fill="both", expand=True)
        self.lv_tree.bind("<<TreeviewSelect>>", self._lv_select)
        self.lv_tree.tag_configure("bad", foreground="#c92a2a")
        ttk.Label(left, text="Flatness = highest minus lowest point (mm).\nBelow ~0.3 mm is normal for this printer.",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=4)
        pw.add(left, weight=1)
        right = ttk.Frame(pw)
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        self.lv_title = ttk.Label(bar, text="", style="H.TLabel")
        self.lv_title.pack(side="left")
        self.lv_img_btn = ttk.Button(bar, text="Open printer's 3D image", command=self._lv_open_image)
        self.lv_img_btn.pack(side="right")
        self.lv_zoom = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Zoom to probed area", variable=self.lv_zoom,
                        command=self._lv_draw).pack(side="right", padx=10)
        self.lv_canvas = tk.Canvas(right, background="#ffffff", highlightthickness=0)
        self.lv_canvas.pack(fill="both", expand=True, pady=6)
        self.lv_canvas.bind("<Configure>", lambda e: self._lv_draw())
        self.lv_stats = ttk.Label(right, text="", justify="left")
        self.lv_stats.pack(fill="x")
        pw.add(right, weight=3)
        self._lv_run: P.LevelingRun | None = None

    def _fill_leveling(self):
        t = self.lv_tree
        t.delete(*t.get_children())
        for i, r in enumerate(self.data.leveling):
            fl = r.data.get("flatness", 0) or 0
            t.insert("", "end", iid=str(i), tags=("bad",) if fl > 0.4 else (), values=(
                fmt_dt(r.time), r.mode_name, len(r.points), f"{fl:.3f}", f"{r.data.get('rmsError', 0):.3f}"))
        if self.data.leveling:
            last = str(len(self.data.leveling) - 1)
            t.selection_set(last)
            t.see(last)
        else:
            self._lv_run = None
            self._lv_draw()

    def _lv_select(self, _e=None):
        sel = self.lv_tree.selection()
        if not sel:
            return
        self._lv_run = self.data.leveling[int(sel[0])]
        r = self._lv_run
        dd = r.data
        self.lv_title.config(text=f"Leveling {fmt_dt(r.time, True)} – {r.mode_name}")
        self.lv_img_btn.state(["!disabled"] if r.image_path else ["disabled"])
        self.lv_stats.config(text=(
            f"Flatness {dd.get('flatness', 0):.3f} mm · lowest {dd.get('minDeviation', 0):+.3f} mm · "
            f"highest {dd.get('maxDeviation', 0):+.3f} mm · mean {dd.get('meanError', 0):+.3f} mm · "
            f"RMS {dd.get('rmsError', 0):.3f} mm\n"
            f"Probe area X {dd.get('range_xmin')}–{dd.get('range_xmax')} mm, Y {dd.get('range_ymin')}–{dd.get('range_ymax')} mm · "
            f"bed temp {dd.get('start_temp', '?')} °C · axis angles XY {dd.get('xy_angle', '')}, "
            f"XZ {dd.get('xz_angle', '')}, YZ {dd.get('yz_angle', '')}"))
        self._lv_draw()

    def _lv_draw(self):
        c = self.lv_canvas
        c.delete("all")
        r = self._lv_run
        W, H = c.winfo_width(), c.winfo_height()
        if not r or W < 50:
            c.create_text(W / 2, H / 2, text="No leveling data", fill="#868e96")
            return
        cfg = self.data.configs.get("e2config.cfg", {})
        bx, by = float(cfg.get("x_max_travel", 330)), float(cfg.get("y_max_travel", 240))
        pts = r.points
        if not pts:
            return
        xs = sorted({p[0] for p in pts})
        ys = sorted({p[1] for p in pts})
        dx = min((b - a for a, b in zip(xs, xs[1:])), default=10)
        dy = min((b - a for a, b in zip(ys, ys[1:])), default=10)
        if self.lv_zoom.get():
            vx0, vx1 = max(0, xs[0] - dx), min(bx, xs[-1] + dx)
            vy0, vy1 = max(0, ys[0] - dy), min(by, ys[-1] + dy)
        else:
            vx0, vx1, vy0, vy1 = 0, bx, 0, by
        pad, legend_w = 44, 100
        scale = min((W - 2 * pad - legend_w) / (vx1 - vx0), (H - 2 * pad) / (vy1 - vy0))
        ox, oy = pad, pad + (vy1 - vy0) * scale

        def X(x):
            return ox + (x - vx0) * scale

        def Y(y):
            return oy - (y - vy0) * scale
        c.create_rectangle(X(max(vx0, 0)), Y(min(vy1, by)), X(min(vx1, bx)), Y(max(vy0, 0)),
                           fill="#f1f3f5", outline="#adb5bd", width=2)
        f8 = ("Segoe UI", 8)
        c.create_text(X(vx0), Y(vy0) + 6, text=f"X {vx0:.0f}", anchor="n", fill="#868e96", font=f8)
        c.create_text(X(vx1), Y(vy0) + 6, text=f"X {vx1:.0f}", anchor="n", fill="#868e96", font=f8)
        c.create_text(X(vx0) - 6, Y(vy1), text=f"Y {vy1:.0f}", anchor="e", fill="#868e96", font=f8)
        c.create_text(X(vx0) - 6, Y(vy0), text=f"Y {vy0:.0f}", anchor="e", fill="#868e96", font=f8)
        c.create_text(X((vx0 + vx1) / 2), Y(vy1) - 8, text="↑ back of printer", anchor="s", fill="#adb5bd", font=f8)
        c.create_text(X((vx0 + vx1) / 2), Y(vy0) + 20, text="front (door) ↓", anchor="n", fill="#adb5bd", font=f8)
        mean = sum(p[2] for p in pts) / len(pts)
        vmax = max(abs(p[2] - mean) for p in pts) or 0.01
        vmax = max(vmax, 0.05)
        show_text = dx * scale > 34 and dy * scale > 16
        for x, y, v in pts:
            col = diverging_color(v - mean, vmax)
            c.create_rectangle(X(x - dx / 2), Y(y + dy / 2), X(x + dx / 2), Y(y - dy / 2), fill=col, outline="#ffffff")
            if show_text:
                c.create_text(X(x), Y(y), text=f"{v:+.2f}", font=("Segoe UI", 8), fill="#212529")
        # legend
        lx = W - legend_w + 20
        top, bot = pad, pad + min(by * scale, 260)
        steps = 40
        for i in range(steps):
            f = 1 - 2 * i / (steps - 1)
            y0 = top + (bot - top) * i / steps
            c.create_rectangle(lx, y0, lx + 18, y0 + (bot - top) / steps + 1, fill=diverging_color(f * vmax, vmax), outline="")
        c.create_text(lx + 24, top, text=f"+{vmax:.2f}", anchor="w", font=("Segoe UI", 8))
        c.create_text(lx + 24, (top + bot) / 2, text=f"avg {mean:+.2f}", anchor="w", font=("Segoe UI", 8))
        c.create_text(lx + 24, bot, text=f"−{vmax:.2f}", anchor="w", font=("Segoe UI", 8))
        c.create_text(lx, bot + 14, text="mm vs. average\nred = high\nblue = low", anchor="nw",
                      font=("Segoe UI", 8), fill="#868e96")

    def _lv_open_image(self):
        r = self._lv_run
        if not r or not r.image_path:
            return
        self._open_external(r.image_path)

    def _open_external(self, path: str):
        out = os.path.join(self._tmpdir, path.replace("/", "_"))
        with open(out, "wb") as fh:
            fh.write(self.data.bundle.raw(path))
        try:
            os.startfile(out)  # type: ignore[attr-defined]
        except AttributeError:
            import subprocess
            subprocess.Popen(["xdg-open" if sys.platform.startswith("linux") else "open", out])

    # ======================================================================
    # Log files
    # ======================================================================
    _ERR_RX = re.compile(r"\b(error|fail(ed|ure)?|shutdown|exception|traceback|fatal|critical|panic)\b", re.I)
    _WARN_RX = re.compile(r"\b(warn(ing)?|timeout|lost|disconnect(ed)?|not found|invalid|without target)\b", re.I)

    def _build_files(self):
        f = self.tabs["Log files"]
        pw = ttk.PanedWindow(f, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw)
        fr, self.fl_tree = make_tree(left, ["Size"], [70], {"Size": "e"}, height=25, show="tree headings")
        self.fl_tree.heading("#0", text="File")
        self.fl_tree.column("#0", width=260)
        fr.pack(fill="both", expand=True)
        self.fl_tree.bind("<<TreeviewSelect>>", lambda e: self._fl_select())
        pw.add(left, weight=1)
        right = ttk.Frame(pw)
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        ttk.Label(bar, text="Find:").pack(side="left")
        self.fl_find = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.fl_find, width=30)
        ent.pack(side="left", padx=4)
        ent.bind("<Return>", lambda e: self._fl_find_next())
        ttk.Button(bar, text="Next", command=self._fl_find_next).pack(side="left")
        ttk.Button(bar, text="Prev", command=lambda: self._fl_find_next(back=True)).pack(side="left", padx=2)
        self.fl_regex = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Regex", variable=self.fl_regex).pack(side="left", padx=6)
        self.fl_only = tk.StringVar(value="All lines")
        cb = ttk.Combobox(bar, textvariable=self.fl_only, state="readonly", width=26,
                          values=["All lines", "Only lines matching 'Find'", "Only errors / warnings",
                                  "Hide temperature status lines"])
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._fl_render())
        self.fl_count = ttk.Label(bar, text="", style="Muted.TLabel")
        self.fl_count.pack(side="right")
        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True, pady=4)
        self.fl_text = tk.Text(tf, wrap="none", font=MONO, background="#ffffff", undo=False)
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.fl_text.yview)
        xs = ttk.Scrollbar(tf, orient="horizontal", command=self.fl_text.xview)
        self.fl_text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.fl_text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)
        self.fl_text.tag_configure("err", foreground="#c92a2a", background="#fff5f5")
        self.fl_text.tag_configure("warn", foreground="#a35200", background="#fff9db")
        self.fl_text.tag_configure("hit", background="#ffe066")
        self.fl_text.tag_configure("cur", background="#ff922b")
        self.fl_text.tag_configure("goto", background="#d0ebff")
        self.fl_text.tag_configure("ln", foreground="#adb5bd")
        self.fl_text.bind("<Key>", lambda e: None if (e.state & 4 and e.keysym.lower() in ("c", "a")) else "break")
        pw.add(right, weight=4)
        self._fl_path = None
        self._fl_lines: list[str] = []

    def _fill_files(self):
        t = self.fl_tree
        t.delete(*t.get_children())
        folders: dict[str, str] = {}
        for p in sorted(self.data.bundle.files):
            parts = p.split("/")
            parent = ""
            for i, part in enumerate(parts[:-1]):
                key = "/".join(parts[:i + 1])
                if key not in folders:
                    folders[key] = t.insert(folders.get("/".join(parts[:i]), ""), "end", iid="dir:" + key,
                                            text="📁 " + part, open=(part in ("raisetouch-system",)))
                parent = folders[key]
            size = len(self.data.bundle.files[p])
            t.insert(parent, "end", iid="file:" + p, text=parts[-1], values=(self._size(size),))
        self._fl_path = None
        self._fl_set_text("Select a file on the left.\n\nUseful files:\n"
                          "  raisetouch-system/0.log … 4.log  – serial communication with the motion board "
                          "(temperatures every second, firmware messages)\n"
                          "  raisetouch-system/klippy.log     – Klipper firmware log (errors, shutdowns, filament sensor)\n"
                          "  raisetouch-system/mxcui.log      – touch-screen application log\n"
                          "  raisetouch-system/mxc-timeline.log – print/leveling/filament timeline\n"
                          "  device-sys/syslog, kern.log      – Linux system logs\n")

    @staticmethod
    def _size(n):
        for unit in ("B", "KB", "MB"):
            if n < 1024 or unit == "MB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024

    def _fl_set_text(self, s):
        self.fl_text.delete("1.0", "end")
        self.fl_text.insert("1.0", s)
        self.fl_count.config(text="")

    def _fl_select(self):
        sel = self.fl_tree.selection()
        if not sel or not sel[0].startswith("file:"):
            return
        self._fl_load(sel[0][5:])

    def _fl_load(self, path):
        self._fl_path = path
        b = self.data.bundle
        raw = b.raw(path)
        if path.lower().endswith((".jpg", ".png", ".jpeg")):
            self._fl_lines = []
            self._fl_set_text(f"{path} is an image.\nIt has been opened in your image viewer.")
            self._open_external(path)
            return
        if path.endswith(".db"):
            self._fl_lines = []
            self._fl_set_text(f"{path} is a SQLite database with encoded print records.\n"
                              "Its contents are decoded and shown in the 'Print history' tab.")
            return
        if path.endswith(".dat") and raw[:1] == b"[":
            self._fl_lines = []
            self._fl_set_text(f"{path} holds encoded print records (decoded in the 'Print history' tab).")
            return
        if P.Bundle.is_binary(raw):
            self._fl_lines = []
            self._fl_set_text(f"{path} is a binary file ({len(raw)} bytes) – not viewable as text "
                              "(e.g. wtmp/lastlog login records).")
            return
        self._fl_lines = raw.decode("utf-8", errors="replace").splitlines()
        self._fl_render()

    def _fl_pattern(self):
        q = self.fl_find.get()
        if not q:
            return None
        try:
            return re.compile(q if self.fl_regex.get() else re.escape(q), re.I)
        except re.error:
            return None

    def _fl_render(self, goto_line: int | None = None):
        if self._fl_path is None or not self._fl_lines:
            return
        mode = self.fl_only.get()
        pat = self._fl_pattern()
        rows = []
        for i, line in enumerate(self._fl_lines, 1):
            if mode == "Only lines matching 'Find'" and pat and not pat.search(line):
                continue
            if mode == "Only errors / warnings" and not (self._ERR_RX.search(line) or self._WARN_RX.search(line)):
                continue
            if mode == "Hide temperature status lines" and ("serial: ok T:" in line or "CameraProcessTask" in line):
                continue
            rows.append((i, line))
        txt = self.fl_text
        txt.delete("1.0", "end")
        width = len(str(len(self._fl_lines)))
        txt.insert("1.0", "\n".join(f"{i:>{width}}  {line}" for i, line in rows))
        txt.tag_add("ln", "1.0", "1.0")
        tagged = 0
        for k, (_i, line) in enumerate(rows, 1):
            if tagged > 20000:
                break
            if self._ERR_RX.search(line) and "serial: ok T:" not in line:
                txt.tag_add("err", f"{k}.0", f"{k}.end")
                tagged += 1
            elif self._WARN_RX.search(line) and "serial: ok T:" not in line:
                txt.tag_add("warn", f"{k}.0", f"{k}.end")
                tagged += 1
        self._fl_rows = rows
        self.fl_count.config(text=f"{len(rows):,} of {len(self._fl_lines):,} lines · {self._fl_path}")
        self._fl_highlight()
        if goto_line:
            for k, (i, _l) in enumerate(rows, 1):
                if i >= goto_line:
                    txt.tag_add("goto", f"{k}.0", f"{k}.end+1c")
                    txt.see(f"{k}.0")
                    txt.mark_set("insert", f"{k}.0")
                    break

    def _fl_highlight(self):
        txt = self.fl_text
        txt.tag_remove("hit", "1.0", "end")
        pat = self._fl_pattern()
        if not pat:
            return
        n = 0
        for k, (_i, line) in enumerate(getattr(self, "_fl_rows", []), 1):
            for m in pat.finditer(line):
                off = len(str(len(self._fl_lines))) + 2
                txt.tag_add("hit", f"{k}.{m.start() + off}", f"{k}.{m.end() + off}")
                n += 1
                if n > 5000:
                    return

    def _fl_find_next(self, back=False):
        if self.fl_only.get() == "Only lines matching 'Find'":
            self._fl_render()
        else:
            self._fl_highlight()
        txt = self.fl_text
        txt.tag_remove("cur", "1.0", "end")
        start = txt.index("insert")
        if back:
            rng = txt.tag_prevrange("hit", start)
            if not rng:
                rng = txt.tag_prevrange("hit", "end")
        else:
            rng = txt.tag_nextrange("hit", start + "+1c")
            if not rng:
                rng = txt.tag_nextrange("hit", "1.0")
        if rng:
            txt.tag_add("cur", *rng)
            txt.mark_set("insert", rng[0])
            txt.see(rng[0])

    def open_log_file(self, path: str, line: int = 0):
        self.nb.select(self.tabs["Log files"])
        iid = "file:" + path
        if self.fl_tree.exists(iid):
            parent = self.fl_tree.parent(iid)
            while parent:
                self.fl_tree.item(parent, open=True)
                parent = self.fl_tree.parent(parent)
            self.fl_tree.see(iid)
        self.fl_only.set("All lines")
        self._fl_path = path
        self._fl_lines = self.data.bundle.raw(path).decode("utf-8", errors="replace").splitlines()
        self._fl_render(goto_line=line)

    # ======================================================================
    # Settings
    # ======================================================================
    def _build_settings(self):
        f = self.tabs["Settings"]
        ttk.Label(f, text="Printer configuration files (passwords and keys are hidden)", style="Muted.TLabel").pack(anchor="w")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=4)
        ttk.Label(bar, text="Filter:").pack(side="left")
        self.st_filter = tk.StringVar()
        ttk.Entry(bar, textvariable=self.st_filter, width=30).pack(side="left", padx=4)
        self.st_filter.trace_add("write", lambda *_: self._fill_settings())
        fr, self.st_tree = make_tree(f, ["Value"], [500], height=25, show="tree headings")
        self.st_tree.heading("#0", text="Setting")
        self.st_tree.column("#0", width=380)
        fr.pack(fill="both", expand=True)

    def _fill_settings(self):
        if not self.data:
            return
        t = self.st_tree
        t.delete(*t.get_children())
        q = self.st_filter.get().lower().strip()
        for name, cfg in sorted(self.data.configs.items()):
            items = [(k, v) for k, v in sorted(cfg.items()) if not q or q in k.lower() or q in str(v).lower()]
            if not items:
                continue
            node = t.insert("", "end", text=name, open=bool(q) or name in ("printer.cfg",))
            for k, v in items:
                v = P.mask_value(k, v)
                if isinstance(v, float):
                    v = f"{v:.4g}"
                elif isinstance(v, (list, dict)):
                    v = f"{len(v)} item(s)" if v else "(empty)"
                t.insert(node, "end", text=k, values=(str(v),))

    # ======================================================================
    # Misc
    # ======================================================================
    def export_report(self):
        if not self.data:
            return
        p = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")],
                                         initialfile="printer_findings.txt")
        if not p:
            return
        d = self.data
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"{APP_TITLE} – findings report\n")
            fh.write(f"Printer: {d.info.get('printer_type')} serial {d.info.get('serial_no')}, "
                     f"RaiseTouch {d.info.get('raisetouch_version')}, firmware {d.info.get('firmware_version')}\n")
            fh.write(f"Log archive: {os.path.basename(d.bundle.source)}\n\n")
            for x in d.findings:
                fh.write(f"[{x.severity.upper()}] {x.title}\n")
                if x.time:
                    fh.write(f"  When: {fmt_dt(x.time, True)}" + (f" -> {fmt_dt(x.time_end, True)}" if x.time_end else "") + "\n")
                fh.write("  " + x.detail + "\n\n")
        self.status.config(text=f"Report saved to {p}")

    def show_help(self):
        messagebox.showinfo("How to use", (
            "1. On the printer touch screen export the logs (Settings → Machine → Export logs) to a USB stick.\n"
            "2. Open the all-raisetouch-….tar.gz file here (File → Open, or put it next to this program).\n\n"
            "Tabs:\n"
            "• Overview – key numbers and automatically detected problems.\n"
            "• Print history – every print job with result, time and filament. Click a job for details.\n"
            "• Temperatures – nozzle and bed temperatures (last ~24 h kept by the printer).\n"
            "• Timeline – all events (prints, errors, shutdowns, filament sensor, leveling) in one list.\n"
            "• Bed leveling – height map of each automatic bed leveling.\n"
            "• Log files – raw log files with search and error highlighting.\n"
            "• Settings – the printer's configuration."))
