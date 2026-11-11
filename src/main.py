"""Start the DSD to FLAC window."""

import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui import App  # noqa: E402


def _sharp_text_on_windows() -> None:
    """Ask Windows not to blur the window on high-DPI screens."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def run() -> None:
    _sharp_text_on_windows()
    App().mainloop()


if __name__ == "__main__":
    run()
