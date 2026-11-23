"""Raise3D E3 Log Viewer - start with:  python main.py [log-archive.tar.gz]"""
import sys

from r3dlog.gui import App

if __name__ == "__main__":
    try:  # crisp text on high-DPI Windows screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()
