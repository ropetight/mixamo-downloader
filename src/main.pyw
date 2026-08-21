"""Entry point of the Mixamo Downloader."""

# Stdlib modules
import gc
import os
import signal
import sys

# Make sure the local modules are importable no matter where the application
# was launched from (double-click, terminal, frozen build).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Third-party modules
from PySide6 import QtWidgets

# Local modules
from appexit import force_exit_after, shutdown_qt
from ui import MixamoDownloaderUI


def main():
    """Launch the application and shut it down cleanly.

    :return: Process exit code
    :rtype: int
    """
    # Ctrl+C is otherwise swallowed by the Qt event loop, leaving the only
    # way out a kill from another terminal. Supported on Windows too.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("MixamoDownloader")
    app.setOrganizationName("MixamoDownloader")
    app.setQuitOnLastWindowClosed(True)

    window = MixamoDownloaderUI()
    window.show()

    code = app.exec()

    # Tear the window down while the application is still alive, then drop
    # the last reference to the application itself so Qt runs its own
    # shutdown rather than leaving it to interpreter teardown.
    shutdown_qt(app, window)

    del window
    del app
    gc.collect()

    return code


if __name__ == "__main__":
    exit_code = main()

    # Everything above is the graceful path, and on a healthy shutdown the
    # interpreter exits within milliseconds -- letting atexit handlers, and a
    # frozen build's cleanup of its unpacked temporary folder, run normally.
    # The watchdog is a daemon thread, so it only ever fires if QtWebEngine
    # has left a thread behind that would strand the process.
    force_exit_after(code=exit_code)

    sys.exit(exit_code)
