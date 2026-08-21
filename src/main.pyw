"""Entry point of the Mixamo Downloader."""

# Stdlib modules
import os
import signal
import sys

# Make sure the local modules are importable no matter where the application
# was launched from (double-click, terminal, frozen build).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Third-party modules
from PySide6 import QtWidgets

# Local modules
from ui import MixamoDownloaderUI


def main():
    """Launch the application.

    :return: Process exit code
    :rtype: int
    """
    # Without this, Ctrl+C in the terminal is swallowed by the Qt event loop
    # and the only way out is to kill the process.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("MixamoDownloader")
    app.setOrganizationName("MixamoDownloader")
    app.setQuitOnLastWindowClosed(True)

    window = MixamoDownloaderUI()
    window.show()

    code = app.exec()

    # Tear the window down while the application object is still alive:
    # QtWebEngine needs its view and page destroyed before its render
    # processes will exit.
    window.shutdown()
    app.processEvents()
    del window

    return code


if __name__ == "__main__":
    exit_code = main()

    # QtWebEngine leaves helper threads behind that can outlive the event
    # loop and keep the interpreter from returning to the shell. Everything
    # this application owns has been closed by now -- downloads are written
    # and fsynced as they finish -- so leaving the rest to the kernel is
    # safe, and it guarantees the command prompt actually comes back.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
