"""Locating the files shipped alongside the application.

Running from source, those files sit next to the sources. In a frozen build
they are unpacked somewhere else entirely -- PyInstaller points at the
directory through `sys._MEIPASS` -- so a path built from `__file__` alone
finds nothing and the animation list fails to load.
"""

# Stdlib modules
import os
import sys


# Where the sources live when the application is not frozen: the folder that
# holds main.pyw, mixamo_anims.json and the icon.
SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_dirs():
    """List the folders a shipped file might be in, best guess first.

    :return: Candidate directories
    :rtype: list
    """
    candidates = []

    # PyInstaller (and similar) unpack data files here.
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(bundle)

    # Next to the executable, for a build that ships its data unpacked.
    if getattr(sys, "frozen", False):
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))

    candidates.append(SOURCE_DIR)

    return candidates


def resource_path(name):
    """Find a file shipped with the application.

    :param name: File name, as shipped
    :type name: str

    :return: Path to the first candidate that exists, or the source-tree
        path when none do, so callers report a sensible missing-file error
    :rtype: str
    """
    for directory in resource_dirs():
        candidate = os.path.join(directory, name)
        if os.path.exists(candidate):
            return candidate

    return os.path.join(SOURCE_DIR, name)
