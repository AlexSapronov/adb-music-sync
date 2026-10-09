"""Entry point.

Works both as ``python -m adb_music_sync`` and as a PyInstaller-frozen
executable. The frozen .exe is built by pointing PyInstaller at this script
directly (no package context), so we use ABSOLUTE imports here — a relative
``from .gui import run`` breaks under PyInstaller with
``ImportError: attempted relative import with no known parent package``.

``adb-music-sync --smoke-test`` runs a headless runtime self-check (imports the
core modules and verifies PySide6/Qt loads) and exits 0 on success, without
showing a GUI or touching ADB. Used by CI to prove the final frozen artifact
actually runs, not just that it was produced.
"""

from __future__ import annotations

import sys

_SMOKE_FLAG = "--smoke-test"


def _smoke_test() -> int:
    """Load the GUI runtime, platform plugin, widgets and file dialog.

    A --windowed frozen exe has sys.stdout == None, so we must not rely on
    print: a bare print would crash before returning. The result is conveyed
    purely by the exit code (0 = ok).
    """
    # Verify PySide6/Qt actually loads (proves plugins/runtime are bundled).
    from PySide6.QtWidgets import QApplication, QFileDialog

    import adb_music_sync  # noqa: F401
    import adb_music_sync.adb  # noqa: F401
    import adb_music_sync.controller  # noqa: F401
    import adb_music_sync.gui  # noqa: F401
    import adb_music_sync.storage  # noqa: F401
    import adb_music_sync.transfer  # noqa: F401

    try:
        app = QApplication.instance() or QApplication(sys.argv)
        # Load the real Qt platform plugin (qwindows on Windows) and construct
        # widgets/dialogs without showing windows or probing ADB.
        from adb_music_sync.controller import Controller
        from adb_music_sync.gui import MainWindow

        widget = MainWindow(Controller())
        dialog = QFileDialog(widget)
        widget.ensurePolished()
        dialog.ensurePolished()
        app.processEvents()
        return 0
    except Exception:
        return 1


def main() -> int:
    if _SMOKE_FLAG in sys.argv[1:]:
        return _smoke_test()

    # Import here so `python -m adb_music_sync` is cheap and GUI-free on import.
    from adb_music_sync.gui import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
