"""
Parsers for the diagnostic log bundle exported by a Raise3D E3 printer
(RaiseTouch "Export logs" -> all-raisetouch-<pid>-<timestamp>.tar.gz).

The outer archive contains one .tar.gz per category:
    device-sys        Linux system logs (syslog, kern.log, ...)
    raisetouch-cloud  cloud connector logs
    raisetouch-configs printer configuration (*.cfg, JSON)
    raisetouch-print  print history (SQLite + .dat, binary records)
    raisetouch-sensor bed-leveling results (JSON) + 3D images
    raisetouch-system serial/Klipper/UI logs

Everything here uses only the Python standard library.
"""
from __future__ import annotations

import base64
import calendar
import gzip
import io
import json
import os
import re
import sqlite3
import struct
import tarfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# --------------------------------------------------------------------------
# Bundle loading
# --------------------------------------------------------------------------

_SECRET_KEYS = re.compile(r"pass|password|digest|secret|token|key", re.I)


class Bundle:
    """All files of a log export, held in memory as {relative_path: bytes}."""

    def __init__(self, source: str):
        self.source = source
        self.files: dict[str, bytes] = {}
        self.export_time: datetime | None = None
        m = re.search(r"(\d{14})", os.path.basename(source))
        if m:
            try:
                self.export_time = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
            except ValueError:
                pass
        if os.path.isdir(source):
            self._load_dir(source)
        else:
            with open(source, "rb") as f:
                self._load_tar_bytes(f.read(), "")
        if not self.files:
            raise ValueError("No log files found in " + source)

    # -- loading helpers ---------------------------------------------------
    @staticmethod
    def _category(name: str) -> str:
        base = os.path.basename(name)
        m = re.match(r"(.+?)-\d+-\d{14}\.tar(\.gz)?$", base)
        if m:
            return m.group(1)
        return re.sub(r"\.tar(\.gz)?$|\.tgz$", "", base)

    def _add(self, path: str, data: bytes):
        path = path.replace("\\", "/").lstrip("./")
        if re.search(r"\.(tar\.gz|tgz|tar)$", path):
            parent = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
            self._load_tar_bytes(data, parent + self._category(path))
        else:
            self.files[path] = data

    def _load_tar_bytes(self, data: bytes, prefix: str):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                f = tf.extractfile(m)
                if f is None:
                    continue
                name = m.name.replace("\\", "/").lstrip("./")
                self._add(f"{prefix}/{name}" if prefix else name, f.read())

    def _load_dir(self, root: str):
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if os.path.getsize(full) > 200 * 1024 * 1024:
                    continue
                with open(full, "rb") as f:
                    self._add(rel, f.read())

    # -- access ------------------------------------------------------------
    def find(self, pattern: str) -> list[str]:
        rx = re.compile(pattern)
        return sorted(p for p in self.files if rx.search(p))

    def first(self, pattern: str) -> str | None:
        r = self.find(pattern)
        return r[0] if r else None

    def raw(self, path: str) -> bytes:
        data = self.files[path]
        if path.endswith(".gz"):
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        return data

    def text(self, path: str) -> str:
        return self.raw(path).decode("utf-8", errors="replace")

    @staticmethod
    def is_binary(data: bytes) -> bool:
        sample = data[:4096]
        if not sample:
            return False
        if b"\x00" in sample:
            return True
        bad = sum(1 for b in sample if b < 9 or (13 < b < 32))
        return bad / len(sample) > 0.1


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------

EVENT_NAMES = {
    1: "Job loaded",
    10: "Job loaded (remote)",
    19: "Start / heating",
    20: "Printing",
    2: "Pause requested",
    21: "Paused",
    3: "Resumed",
    4: "Stop requested",
    22: "Stopped",
    23: "Completed",
}


@dataclass
class PrintEvent:
    row_id: int
    time: datetime | None
    code: int
    name: str
    used_left_mm: float
    used_right_mm: float
    est_left_g: float
    est_right_g: float
    lines_done: int
    lines_total: int
    start: datetime | None
    end: datetime | None
    elapsed_s: int
    est_s: int
    file_size: int
    file_path: str
    job_id: str
    cloud_id: str


@dataclass
class PrintJob:
    job_id: str
    file_path: str
    events: list[PrintEvent] = field(default_factory=list)

    @property
    def file_name(self) -> str:
        return self.file_path.replace("\\", "/").split("/")[-1]

    @property
    def codes(self) -> list[int]:
        return [e.code for e in self.events]

    @property
    def last(self) -> PrintEvent:
        return self.events[-1]

    @property
    def result(self) -> str:
        c = self.codes
        if 23 in c:
            return "Completed"
        if 22 in c or 4 in c:
            return "Stopped"
        if 21 in c and 3 not in c:
            return "Paused"
        return "No end record"

    @property
    def pauses(self) -> int:
        return self.codes.count(21)

    @property
    def start(self) -> datetime | None:
        for e in self.events:
            if e.start:
                return e.start
        return self.events[0].time

    @property
    def end(self) -> datetime | None:
        for e in reversed(self.events):
            if e.code in (22, 23) and e.end:
                return e.end
        return None

    @property
    def elapsed_s(self) -> int:
        return max((e.elapsed_s for e in self.events), default=0)

    @property
    def est_s(self) -> int:
        return max((e.est_s for e in self.events), default=0)

    @property
    def used_m(self) -> tuple[float, float]:
        l = max((e.used_left_mm for e in self.events), default=0.0)
        r = max((e.used_right_mm for e in self.events), default=0.0)
        return l / 1000.0, r / 1000.0

    @property
    def est_g(self) -> tuple[float, float]:
        l = max((e.est_left_g for e in self.events), default=0.0)
        r = max((e.est_right_g for e in self.events), default=0.0)
        return l, r

    @property
    def progress(self) -> float | None:
        tot = max((e.lines_total for e in self.events), default=0)
        done = max((e.lines_done for e in self.events), default=0)
        if self.result == "Completed":
            return 100.0
        if tot and done:
            return 100.0 * done / tot
        if self.est_s and self.elapsed_s:
            return min(99.0, 100.0 * self.elapsed_s / self.est_s)
        return 0.0 if tot else None

    @property
    def extruders(self) -> str:
        l, r = self.used_m
        gl, gr = self.est_g
        left = l > 0 or gl > 0
        right = r > 0 or gr > 0
        if left and right:
            return "Both"
        if right:
            return "Right"
        if left:
            return "Left"
        return "-"

    @property
    def is_calibration(self) -> bool:
        return "/calibration/" in self.file_path or "calibration" in self.file_name.lower()


@dataclass
class TempSeries:
    """Temperature samples from the serial log (1 sample/second)."""
    t: list[float] = field(default_factory=list)          # epoch seconds (local)
    channels: dict[str, list[float]] = field(default_factory=dict)

    CHANNELS = [
        ("T0", "Left nozzle"),
        ("T0_target", "Left nozzle target"),
        ("T1", "Right nozzle"),
        ("T1_target", "Right nozzle target"),
        ("B", "Bed"),
        ("B_target", "Bed target"),
    ]


@dataclass
class LogEvent:
    time: datetime
    source: str
    category: str
    severity: str      # "info" | "warning" | "error"
    message: str
    file: str = ""
    line: int = 0


@dataclass
class Finding:
    severity: str      # "critical" | "warning" | "info"
    title: str
    detail: str
    time: datetime | None = None
    time_end: datetime | None = None
    tab: str = ""      # GUI tab to jump to


@dataclass
class LevelingRun:
    data: dict
    image_path: str | None = None

    @property
    def time(self) -> datetime | None:
        try:
            return datetime.strptime(self.data.get("end_time", ""), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @property
    def points(self) -> list[tuple[float, float, float]]:
        return [(p["x"], p["y"], p["offset"]) for p in self.data.get("templates", [])]

    @property
    def mode_name(self) -> str:
        m = self.data.get("levelling_mode")
        return {1: "Full bed", 2: "Auto", 3: "Print area"}.get(m, f"Mode {m}")


# --------------------------------------------------------------------------
# Binary print-record decoding (raisetouch-print)
# --------------------------------------------------------------------------

def _decode_blob(s: str) -> bytes:
    """Records are base64url( uint32 size + zlib(data) )."""
    s = s.strip()
    b = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    return zlib.decompress(b[4:])


def _epoch_to_local(t: int) -> datetime | None:
    if not t or t < 946684800 or t > 4102444800:   # 2000..2100
        return None
    return datetime.fromtimestamp(t)


def parse_print_record(b: bytes) -> dict:
    """Decode one record (type 10001 print event / 10002 print summary)."""
    o = 4
    rtype = struct.unpack_from("<I", b, o)[0]
    o = 18
    code = struct.unpack_from("<I", b, o)[0]; o += 4
    ul, ur, gl, gr = struct.unpack_from("<ffff", b, o); o += 16
    done, total = struct.unpack_from("<II", b, o); o += 8
    o += 8                                   # two extruder odometers (float)
    o += 4                                   # flags
    t_end = struct.unpack_from("<Q", b, o)[0]; o += 8
    elapsed, _remain, est, fsize = struct.unpack_from("<qqqq", b, o); o += 32
    t_start = struct.unpack_from("<Q", b, o)[0]; o += 8
    strings = []
    while o + 4 <= len(b):
        n = struct.unpack_from("<I", b, o)[0]; o += 4
        if n == 0xFFFFFFFF:
            strings.append("")
            continue
        strings.append(b[o:o + n].decode("utf-16-le", errors="replace")); o += n
    return dict(type=rtype, code=code, used_left_mm=ul, used_right_mm=ur,
                est_left_g=gl, est_right_g=gr, lines_done=done, lines_total=total,
                start=_epoch_to_local(t_start), end=_epoch_to_local(t_end),
                elapsed_s=int(elapsed), est_s=int(est), file_size=int(fsize),
                strings=strings)


def _sqlite_rows(data: bytes, query: str) -> list[tuple]:
    con = sqlite3.connect(":memory:")
    try:
        con.deserialize(data)
        return con.execute(query).fetchall()
    finally:
        con.close()


def _utc_str_to_local(s: str) -> datetime | None:
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(calendar.timegm(dt.timetuple()))


def load_print_events(bundle: Bundle) -> list[PrintEvent]:
    rows: list[tuple[int, str, datetime | None]] = []
    db = bundle.first(r"raisetouch-print/print_log\.db$")
    if db:
        try:
            for rid, data, date in _sqlite_rows(bundle.raw(db), "select id, data, date from printlog order by id"):
                rows.append((rid, data, _utc_str_to_local(date)))
        except Exception:
            rows = []
    if not rows:
        dat = bundle.first(r"raisetouch-print/print_log\.dat$")
        if dat:
            for i, blob in enumerate(re.findall(r"\[([^\]]+)\]", bundle.text(dat))):
                rows.append((i + 1, blob, None))
    events = []
    for rid, blob, when in rows:
        try:
            r = parse_print_record(_decode_blob(blob))
        except Exception:
            continue
        s = r["strings"] + ["", "", ""]
        events.append(PrintEvent(
            row_id=rid, time=when or r["end"] or r["start"], code=r["code"],
            name=EVENT_NAMES.get(r["code"], f"Event {r['code']}"),
            used_left_mm=r["used_left_mm"], used_right_mm=r["used_right_mm"],
            est_left_g=r["est_left_g"], est_right_g=r["est_right_g"],
            lines_done=r["lines_done"], lines_total=r["lines_total"],
            start=r["start"], end=r["end"], elapsed_s=r["elapsed_s"], est_s=r["est_s"],
            file_size=r["file_size"], file_path=s[0], job_id=s[1] or s[0], cloud_id=s[2]))
    return events


def build_jobs(events: list[PrintEvent]) -> list[PrintJob]:
    jobs: dict[str, PrintJob] = {}
    for e in events:
        j = jobs.get(e.job_id)
        if j is None:
            j = jobs[e.job_id] = PrintJob(e.job_id, e.file_path)
        j.events.append(e)
    out = list(jobs.values())
    out.sort(key=lambda j: j.start or datetime.min)
    return out


# --------------------------------------------------------------------------
# Text logs
# --------------------------------------------------------------------------

_SERIAL_RX = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)\s*\S*\|\s*([^|]*)\|\s*(.*)$")
_TEMP_RX = re.compile(
    r"T0:(-?[\d.]+) /(-?[\d.]+) T1:(-?[\d.]+) /(-?[\d.]+)")
_BED_RX = re.compile(r"B:(-?[\d.]+) /(-?[\d.]+)")
_KLIPPY_RX = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\]\s?(.*)$")
_MXC_RX = re.compile(r"^\[(\d{6}-\d{6}\.\d+)\]\s?(.*)$")
_TIMELINE_RX = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d(?::\d\d(?:[:.]\d+)?)?)\]\s?(.*)$")


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _parse_timeline_time(s: str) -> datetime | None:
    # "2026-10-16 16:15:43:844" or "2026-10-16 17:12"
    m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d)(?::(\d\d))?(?:[:.](\d+))?", s)
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
    if m.group(2):
        dt = dt.replace(second=int(m.group(2)))
    if m.group(3):
        dt = dt.replace(microsecond=int(m.group(3)[:6].ljust(6, "0")))
    return dt


def _parse_mxc_time(s: str) -> datetime | None:
    try:
        return datetime.strptime(s[:17], "%y%m%d-%H%M%S.%f")
    except ValueError:
        return None


def serial_log_files(bundle: Bundle) -> list[str]:
    """raisetouch-system/N.log rotated serial logs, oldest first."""
    files = bundle.find(r"raisetouch-system/\d+\.log$")

    def first_ts(p):
        for line in bundle.text(p).splitlines()[:5]:
            m = _SERIAL_RX.match(line)
            if m:
                return m.group(1)
        return ""
    return sorted(files, key=first_ts)


def load_serial(bundle: Bundle) -> tuple[TempSeries, list[LogEvent], dict]:
    ts = TempSeries(channels={k: [] for k, _ in TempSeries.CHANNELS})
    events: list[LogEvent] = []
    info: dict = {}
    ch = ts.channels
    for path in serial_log_files(bundle):
        for ln, line in enumerate(bundle.text(path).splitlines(), 1):
            m = _SERIAL_RX.match(line)
            if not m:
                continue
            msg = m.group(3)
            if msg.startswith("serial: ok T:") or msg.startswith("serial: T:"):
                tm = _TEMP_RX.search(msg)
                bm = _BED_RX.search(msg)
                if not tm or not bm:
                    continue
                dt = _parse_iso(m.group(1))
                if dt is None:
                    continue
                ts.t.append(dt.timestamp())
                ch["T0"].append(float(tm.group(1))); ch["T0_target"].append(float(tm.group(2)))
                ch["T1"].append(float(tm.group(3))); ch["T1_target"].append(float(tm.group(4)))
                ch["B"].append(float(bm.group(1))); ch["B_target"].append(float(bm.group(2)))
                continue
            dt = _parse_iso(m.group(1))
            if dt is None:
                continue
            text = msg[len("serial: "):] if msg.startswith("serial: ") else msg
            if not text.strip() or text.strip() in ("ok", "0.0"):
                continue
            sev, cat = "info", "Serial"
            low = text.lower()
            if "shutdown" in low:
                sev, cat = "error", "Shutdown"
            elif low.startswith("error") or "error:" in low:
                sev, cat = "error", "Error"
            elif "without target temp" in low or "temp_raise_with_no_target" in low:
                sev, cat = "warning", "Heater"
            elif text.startswith("Log for RaiseTouch"):
                cat = "Startup"
                mv = re.search(r"version=([^,]*), Firmware version=([^,]*), Printer type=([^ ]+ ?[^ ]*) serialno=(\S+)", text)
                if mv:
                    info["raisetouch_version"] = mv.group(1)
                    if mv.group(2):
                        info["firmware_version"] = mv.group(2)
                    info["serial_no"] = mv.group(4)
            elif text.startswith("MCB V"):
                info["mcb_version"] = text.strip()
                continue
            elif "generally occurs" in low or "configured min_temp" in low:
                continue
            elif text.startswith("Klipper state"):
                cat = "Klipper"
            events.append(LogEvent(dt, "serial", cat, sev, text.strip(), path, ln))
    return ts, events, info


def load_klippy(bundle: Bundle) -> list[LogEvent]:
    out = []
    for path in bundle.find(r"klippy\.log(\.\d+)?$"):
        for ln, line in enumerate(bundle.text(path).splitlines(), 1):
            m = _KLIPPY_RX.match(line)
            if not m:
                continue
            msg = m.group(2).strip()
            low = msg.lower()
            dt = _parse_iso(m.group(1).replace(" ", "T"))
            if dt is None:
                continue
            if "transition to shutdown" in low or ("shutdown:" in low and "mcu" in low):
                out.append(LogEvent(dt, "klippy", "Shutdown", "error", msg, path, ln))
            elif low.startswith("pause filament out"):
                out.append(LogEvent(dt, "klippy", "Filament", "warning", "Print paused: filament run-out (" + msg.split("actExtr:")[-1].split(",")[0] + ")", path, ln))
            elif low.startswith("filament out"):
                out.append(LogEvent(dt, "klippy", "Filament", "info", "Filament sensor: no filament (" + msg.split("actExtr:")[-1].split(",")[0] + ")", path, ln))
            elif "without target temp" in low:
                out.append(LogEvent(dt, "klippy", "Heater", "warning", msg, path, ln))
            elif "lost communication" in low or "timer too close" in low or low.startswith("error"):
                out.append(LogEvent(dt, "klippy", "Error", "error", msg, path, ln))
            elif low.startswith("read port"):
                out.append(LogEvent(dt, "klippy", "Startup", "info", "Klipper started (" + msg.split("-->")[-1] + ")", path, ln))
    return out


def load_timeline(bundle: Bundle) -> list[LogEvent]:
    out = []
    for path in bundle.find(r"mxc-timeline\.log$"):
        for ln, line in enumerate(bundle.text(path).splitlines(), 1):
            m = _TIMELINE_RX.match(line)
            if not m:
                continue
            dt = _parse_timeline_time(m.group(1))
            if dt is None:
                continue
            msg = m.group(2).strip()
            cat, sev = "Timeline", "info"
            if msg.startswith("filament status"):
                cat = "Filament"
                msg = msg.replace("filament status: ", "")
            elif msg.startswith("Print task"):
                cat = "Print"
            elif msg.startswith("Bed leveling"):
                cat = "Leveling"
            elif msg.startswith("z offset"):
                cat = "Calibration"
            out.append(LogEvent(dt, "timeline", cat, sev, msg, path, ln))
    return out


def load_mxc_logs(bundle: Bundle) -> list[LogEvent]:
    """Interesting lines from the RaiseTouch UI / server / monitor logs."""
    out = []
    keep = [
        (re.compile(r'\[MessageBox\] "(.+?)"'), "UI message", "warning"),
        (re.compile(r'\[PrintMachineState\] set state: \d+ "(\w+)"'), "Machine state", "info"),
        (re.compile(r"\[PowerLossTask\] start power task"), "Power", "info"),
        (re.compile(r"\[MXCMonitor\] load (\d+) (\d+)"), "Power", "info"),
    ]
    for path in bundle.find(r"raisetouch-system/mxc(ui|server|monitor|filedaemon)\.log(\.\d+)?$"):
        for ln, line in enumerate(bundle.text(path).splitlines(), 1):
            m = _MXC_RX.match(line)
            if not m:
                continue
            body = m.group(2)
            for rx, cat, sev in keep:
                mm = rx.search(body)
                if not mm:
                    continue
                dt = _parse_mxc_time(m.group(1))
                if dt is None:
                    break
                if cat == "UI message":
                    text = mm.group(1).strip()
                    if not text:
                        break
                    msg = "Screen message: " + text
                elif cat == "Machine state":
                    msg = "Machine state: " + mm.group(1)
                elif rx.pattern.startswith(r"\[MXCMonitor\]"):
                    msg = f"Printer powered on (total on-time {int(mm.group(1))/3600:.1f} h, previous session {int(mm.group(2))/3600:.1f} h)"
                else:
                    msg = "Power-loss monitor started (printer boot)"
                    cat = "Startup"
                out.append(LogEvent(dt, os.path.basename(path), cat, sev, msg, path, ln))
                break
    return out


def load_leveling(bundle: Bundle) -> list[LevelingRun]:
    runs: dict[str, LevelingRun] = {}
    images: dict[str, str] = {}
    for p in bundle.find(r"raisetouch-sensor/.*leveling3D-(\d+)\.(jpg|png)$"):
        images[re.search(r"leveling3D-(\d+)", p).group(1)] = p

    def add_text(text: str, path: str):
        dec = json.JSONDecoder()
        i, n = 0, len(text)
        while i < n:
            while i < n and text[i] in " \r\n\t,":
                i += 1
            if i >= n:
                break
            try:
                obj, i = dec.raw_decode(text, i)
            except ValueError:
                break
            if not isinstance(obj, dict) or "templates" not in obj:
                continue
            key = obj.get("end_time") or obj.get("leveling_hash") or str(len(runs))
            run = runs.get(key) or LevelingRun(obj)
            runs[key] = run
            m = re.search(r"leveling-result-(\d+)\.log$", path)
            if m and m.group(1) in images:
                run.image_path = images[m.group(1)]

    for p in bundle.find(r"raisetouch-sensor/.*leveling-result-all\.log$"):
        add_text(bundle.text(p), p)
    for p in bundle.find(r"raisetouch-sensor/.*leveling-result-\d+\.log$"):
        add_text(bundle.text(p), p)
    # match images by timestamp (MMDDHHMM) for runs that only came from -all.log
    for run in runs.values():
        if run.image_path or not run.time:
            continue
        k = run.time.strftime("%m%d%H%M")
        if k in images:
            run.image_path = images[k]
    out = list(runs.values())
    out.sort(key=lambda r: r.time or datetime.min)
    return out


def load_configs(bundle: Bundle) -> dict[str, dict]:
    out = {}
    for p in bundle.find(r"raisetouch-configs/.*\.cfg$"):
        try:
            out[os.path.basename(p)] = json.loads(bundle.text(p))
        except ValueError:
            out[os.path.basename(p)] = {"(unparsed)": bundle.text(p)[:2000]}
    return out


def mask_value(key: str, value):
    if isinstance(value, str) and value and _SECRET_KEYS.search(key):
        return "••••••"
    if key == "user_name" and isinstance(value, str) and "@" in value:
        name, dom = value.split("@", 1)
        return name[:2] + "•••@" + dom
    return value


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def _fmt_dur(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _spans(t: list[float], mask: list[bool], max_gap: float = 30.0):
    """Yield (i_start, i_end) index spans where mask is True and samples are contiguous."""
    start = None
    for i, ok in enumerate(mask):
        gap = i > 0 and t[i] - t[i - 1] > max_gap
        if start is not None and (not ok or gap):
            yield start, i - 1
            start = None
        if ok and start is None:
            start = i
    if start is not None:
        yield start, len(mask) - 1


def analyze(data: "LogData") -> list[Finding]:
    f: list[Finding] = []
    ts = data.temps
    t = ts.t
    dt = datetime.fromtimestamp
    job_spans = [(j.start.timestamp(), (j.end or (j.start + timedelta(seconds=max(j.elapsed_s, 60)))).timestamp())
                 for j in data.jobs if j.start]

    def printing_at(a: float, b: float) -> bool:
        return any(s < b and e > a for s, e in job_spans)

    heaters = [("B", "Bed", 120.0, 15.0), ("T0", "Left nozzle", 330.0, 15.0), ("T1", "Right nozzle", 330.0, 15.0)]
    if t:
        for key, label, tmax, margin in heaters:
            act = ts.channels[key]
            tgt = ts.channels[key + "_target"]
            # 1) Temperature well above target while a target is set
            mask = [tg > 0 and a > tg + margin and a > -100 for a, tg in zip(act, tgt)]
            for i0, i1 in _spans(t, mask):
                if t[i1] - t[i0] < 60:
                    continue
                peak = max(act[i0:i1 + 1])
                # find where it started rising above target
                j = i0
                while j > 0 and act[j - 1] > tgt[j - 1] + 1 and t[j] - t[j - 1] < 30:
                    j -= 1
                f.append(Finding(
                    "critical", f"{label} overheated far above its target",
                    f"The {label.lower()} rose to {peak:.1f} °C while the target was only {tgt[i0]:.0f} °C "
                    f"(above target from {dt(t[j]):%Y-%m-%d %H:%M}, {_fmt_dur(t[i1]-t[j])}). "
                    "The heater kept heating although the controller wanted it off. This is typical of a failed "
                    "(stuck-on) heater relay/MOSFET or SSR, or a thermistor that is no longer touching the heater. "
                    "Do not leave the printer unattended until a technician checks it.",
                    dt(t[j]), dt(t[i1]), "Temperatures"))
            # 2) Heating with no target at all
            mask = [tg <= 0 for tg in tgt]
            for i0, i1 in _spans(t, mask):
                # sliding 5-min rise detection
                k = i0
                for i in range(i0, i1 + 1):
                    while t[i] - t[k] > 300:
                        k += 1
                    if act[i] > 45 and act[i] - act[k] > 8 and -100 < act[k]:
                        f.append(Finding(
                            "critical", f"{label} heating with no target set",
                            f"The {label.lower()} temperature climbed from {act[k]:.1f} °C to {act[i]:.1f} °C "
                            f"within {_fmt_dur(t[i]-t[k])} although no temperature was requested (target 0 °C). "
                            "Something is powering the heater on its own – possible stuck heater relay/MOSFET.",
                            dt(t[k]), dt(t[i]), "Temperatures"))
                        break
            # 3) Invalid sensor readings
            mask = [a < -50 or a > tmax + 5 for a in act]
            for i0, i1 in _spans(t, mask, max_gap=1e9):
                v = act[i0]
                f.append(Finding(
                    "warning", f"{label} sensor reading invalid ({v:.0f} °C)",
                    f"The {label.lower()} thermistor reported {v:.0f} °C from {dt(t[i0]):%Y-%m-%d %H:%M} to "
                    f"{dt(t[i1]):%H:%M}. Such values mean the sensor is disconnected/shorted or the firmware is in "
                    "a shutdown state.", dt(t[i0]), dt(t[i1]), "Temperatures"))
                break
            # 4) Heater left on for a long time with no print running
            mask = [tg > 0 for tg in tgt]
            for i0, i1 in _spans(t, mask, max_gap=120):
                dur = t[i1] - t[i0]
                if dur >= 2 * 3600 and not printing_at(t[i0], t[i1]):
                    f.append(Finding(
                        "warning", f"{label} kept hot for {_fmt_dur(dur)} with no print running",
                        f"The {label.lower()} target was {tgt[i0]:.0f} °C from {dt(t[i0]):%Y-%m-%d %H:%M} to "
                        f"{dt(t[i1]):%Y-%m-%d %H:%M} but no print job was active. This wastes energy and is a "
                        "safety risk – switch heaters off after use (or enable automatic power saving).",
                        dt(t[i0]), dt(t[i1]), "Temperatures"))

    # 5) Firmware shutdowns (grouped by reason)
    by_reason: dict[str, list[datetime]] = {}
    for e in sorted(data.events, key=lambda e: e.time):
        if e.category != "Shutdown":
            continue
        reason = re.sub(r"^.*shutdown(?: state)?:\s*", "", e.message, flags=re.I).strip() or e.message
        lst = by_reason.setdefault(reason, [])
        if not any(abs((e.time - s).total_seconds()) < 120 for s in lst):
            lst.append(e.time)
    for reason, times in by_reason.items():
        hint = ""
        if "ADC out of range" in reason:
            hint = (" 'ADC out of range' means a temperature sensor reading went outside its allowed range "
                    "(heater far too hot, or a broken/disconnected thermistor).")
        when = ", ".join(f"{t:%Y-%m-%d %H:%M:%S}" for t in times)
        n = f" ({len(times)}×)" if len(times) > 1 else ""
        f.append(Finding("critical", f"Firmware emergency shutdown{n}: {reason}",
                         f"Klipper stopped all heaters and motors at: {when}.{hint}"
                         + (" It happened again after restarting, so the fault is still present." if len(times) > 1 else ""),
                         times[0], times[-1], "Timeline"))

    # 5b) Firmware warnings about heaters rising without a target
    warn = [e for e in data.events if e.category == "Heater" and "without target" in e.message]
    if warn:
        heaters_named = sorted({e.message.split(" temp")[0].split()[-1] for e in warn})
        last = max(warn, key=lambda e: e.time)
        f.append(Finding("critical", "Firmware reports a heater warming up with no target set",
                         f"{len(warn)} message(s) like: \"{last.message}\" (heaters: {', '.join(heaters_named)}; "
                         f"last at {last.time:%Y-%m-%d %H:%M:%S}). A heater that warms although it was not asked to "
                         "points to a stuck-on heater relay/MOSFET.", min(e.time for e in warn), last.time, "Timeline"))

    # 6) Print results
    real = [j for j in data.jobs if not j.is_calibration]
    stopped = [j for j in real if j.result == "Stopped"]
    if stopped:
        lst = ", ".join(f"{j.file_name} ({j.start:%m-%d %H:%M})" for j in stopped[:8])
        f.append(Finding("info", f"{len(stopped)} of {len(real)} print jobs were stopped before finishing",
                         "Stopped jobs: " + lst + ("…" if len(stopped) > 8 else ""), None, None, "Print history"))
    runouts = [e for e in data.events if e.category == "Filament" and e.severity == "warning"]
    if runouts:
        f.append(Finding("info", f"{len(runouts)} pause(s) caused by filament run-out",
                         "The filament sensor paused printing: " +
                         ", ".join(f"{e.time:%m-%d %H:%M}" for e in runouts[:10]), runouts[0].time, None, "Timeline"))
    over = [j for j in real if j.result == "Completed" and j.est_s > 600 and j.elapsed_s > j.est_s * 1.25]
    if over:
        f.append(Finding("info", f"{len(over)} print(s) took >25 % longer than the slicer estimate",
                         ", ".join(f"{j.file_name}: {_fmt_dur(j.elapsed_s)} vs est. {_fmt_dur(j.est_s)}" for j in over[:6]),
                         None, None, "Print history"))

    # 7) Leveling
    for r in data.leveling:
        fl = r.data.get("flatness", 0) or 0
        if fl > 0.4:
            f.append(Finding("warning", f"Bed flatness {fl:.2f} mm in leveling of {r.time:%Y-%m-%d %H:%M}",
                             "A flatness above ~0.4 mm can cause first-layer adhesion problems. Check the build plate "
                             "is seated correctly and clean.", r.time, None, "Bed leveling"))

    # 8) Restarts
    boots = [e for e in data.events if e.category == "Startup" and e.source == "serial"]
    if len(boots) >= 2:
        f.append(Finding("info", f"RaiseTouch was (re)started {len(boots)} times in the serial log window",
                         ", ".join(f"{e.time:%m-%d %H:%M}" for e in boots[:10]), boots[0].time, None, "Timeline"))

    order = {"critical": 0, "warning": 1, "info": 2}
    f.sort(key=lambda x: (order.get(x.severity, 3), x.time or datetime.max))
    return f


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------

@dataclass
class LogData:
    bundle: Bundle
    jobs: list[PrintJob]
    print_events: list[PrintEvent]
    temps: TempSeries
    events: list[LogEvent]
    leveling: list[LevelingRun]
    configs: dict[str, dict]
    info: dict
    findings: list[Finding] = field(default_factory=list)


def load(source: str, progress=lambda msg: None) -> LogData:
    progress("Reading archive…")
    b = Bundle(source)
    progress("Decoding print history…")
    pev = load_print_events(b)
    jobs = build_jobs(pev)
    progress("Parsing serial/temperature logs…")
    temps, sev, info = load_serial(b)
    progress("Parsing Klipper, timeline and UI logs…")
    events = sev + load_klippy(b) + load_timeline(b) + load_mxc_logs(b)
    for j in jobs:
        res = j.result
        if j.start:
            events.append(LogEvent(j.start, "print", "Print", "info", f"Print started: {j.file_name}"))
        if j.end:
            events.append(LogEvent(j.end, "print", "Print", "warning" if res == "Stopped" else "info",
                                   f"Print {res.lower()}: {j.file_name} ({_fmt_dur(j.elapsed_s)})"))
    events.sort(key=lambda e: e.time)
    progress("Reading bed leveling and configuration…")
    lev = load_leveling(b)
    cfg = load_configs(b)
    pc = cfg.get("printer.cfg", {})
    info.setdefault("serial_no", pc.get("printer_serial_number", ""))
    info["printer_type"] = pc.get("printer_type", "Raise3D")
    info["printer_name"] = pc.get("printer_name", "")
    data = LogData(b, jobs, pev, temps, events, lev, cfg, info)
    progress("Analysing…")
    data.findings = analyze(data)
    return data
