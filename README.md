# Raise3D E3 Log Viewer

A desktop app for reading the log export of a Raise3D E3 printer
(`all-raisetouch-<id>-<date>.tar.gz`, made on the printer under *Settings → Machine → Export logs*).

## Starting it on a PC without Python (recommended for school PCs)

1. Copy `dist\Raise3D-E3-Log-Viewer-Windows.zip` to the PC and unzip it (keep the whole folder: the `_internal` folder must stay next to the exe).
2. Run `Raise3D-E3-Log-Viewer.exe`. To open an archive, use File → Open, drag the `.tar.gz` file onto the exe, or put the archive in the same folder.

The first start can take a few seconds while antivirus checks the new program. Windows SmartScreen may say
"Windows protected your PC" because the exe is not code-signed. Click *More info → Run anyway*.

To rebuild after changing the code: `pip install pyinstaller` and then
`pyinstaller --noconfirm --onedir --windowed --name Raise3D-E3-Log-Viewer main.py`

## Starting it from source

You need Python 3.10 or newer (from python.org, with the "tcl/tk" option ticked, which is the default).
Nothing else needs to be installed.

* Double-click **`Start Log Viewer.bat`**, or
* run `python main.py path\to\all-raisetouch-….tar.gz`

If a `all-raisetouch-*.tar.gz` file is in the same folder as the app (or the folder above it), it opens automatically.
You can also use **File → Open log archive…**, or open a folder where the archive has already been extracted.

## Tabs

| Tab | What it shows |
|---|---|
| **Overview** | Key numbers, printer/firmware info, lifetime counters and **automatic findings** (overheating, firmware shutdowns, heaters left on, stopped prints, filament run-outs, bad bed leveling). Double-click a finding to jump to the data. |
| **Print history** | Every print job decoded from the printer's encoded print database: start/end, result (completed / stopped), duration vs. slicer estimate, progress, extruder, filament length and weight, pauses, and the event log of each job. Has filters, a "printing hours per day" chart and CSV export. |
| **Temperatures** | Left/right nozzle and bed temperature plus their targets, one sample per second. Mouse wheel = zoom, drag = pan, right-drag = zoom to a range, double-click = show all. Print jobs are shaded and firmware shutdowns are red lines. |
| **Timeline** | All events from every log in one sorted list (prints, shutdowns, errors, restarts, filament sensor, bed leveling, on-screen messages). Filter by category or severity and search. Double-click an event to open the source log at that line. |
| **Bed leveling** | Height map of every automatic bed leveling (red = high, blue = low), with flatness and RMS. Can open the printer's own 3D image. |
| **Log files** | The raw files (`.gz` files are unpacked automatically), with search (plain or regex), filtering and error/warning highlighting. |
| **Settings** | The printer's configuration files. Passwords and keys are hidden. |

**File → Export findings report** saves the findings as a text file, ready to send to a technician or to Raise3D support.

## Notes and limits

* The printer keeps only about the **last 24 h of temperature data** (5 rotating serial logs) and the recent print history,
  so older data is not in the export.
* Times are shown in the PC's local time zone. It should match the printer's if both are set to the same zone.
* The print database format is not documented. The field meanings (event codes, filament, times) were worked out
  from the data and checked against the other logs. Values marked "Est. weight" are the slicer's plan, not a measurement.
* Some event codes are interpreted: 1/10 = job loaded, 19 = start/heating, 20 = printing, 2/21 = pause, 3 = resume,
  4/22 = stop, 23 = completed. Calibration prints have no end record.

## Code layout

```
main.py            entry point
r3dlog/parser.py   reads the archive and decodes all formats (no GUI code, can be used on its own)
r3dlog/charts.py   time-series and bar charts drawn on a tkinter Canvas
r3dlog/gui.py      the window and tabs
```
