"""Entry point of the Mixamo Downloader."""

# Stdlib modules
import os
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
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("MixamoDownloader")
    app.setOrganizationName("MixamoDownloader")

    window = MixamoDownloaderUI()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
