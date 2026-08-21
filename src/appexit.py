"""Making sure the process ends, on every platform.

Qt applications embedding QtWebEngine can hang on the way out: the event
loop returns, but a helper thread keeps the interpreter from reaching the
end. Killing the process outright avoids that, at the cost of skipping the
cleanup a normal exit does -- which on a frozen Windows build means leaving
the unpacked temporary folder behind.

So the shutdown is graceful first and forceful only if that stalls: the
interpreter is left to exit on its own, and a watchdog steps in only when it
has not managed it in time.
"""

# Stdlib modules
import os
import sys
import threading
import time


# How long a normal interpreter shutdown may take before it is treated as
# stuck. Generous: it should finish in milliseconds.
EXIT_TIMEOUT = 10.0


def force_exit_after(seconds=EXIT_TIMEOUT, code=0, sleep=time.sleep,
                     exit_func=os._exit, warn=None):
    """Start a watchdog that kills the process if shutdown stalls.

    The watchdog runs on a daemon thread, so it never keeps the process
    alive by itself: if the interpreter exits normally first -- the usual
    case -- the thread dies with it and nothing is forced.

    :param seconds: How long to give a normal shutdown
    :type seconds: float

    :param code: Exit code to use if the process has to be killed
    :type code: int

    :param sleep: Sleep function (injected so tests do not wait)
    :type sleep: callable

    :param exit_func: Called to end the process (injected for tests)
    :type exit_func: callable

    :param warn: Called with a message before the process is killed
    :type warn: callable or None

    :return: The watchdog thread, already started
    :rtype: threading.Thread
    """
    def watchdog():
        sleep(seconds)

        # Only reachable while the interpreter is still alive, which means
        # the normal shutdown did not finish.
        (warn or _warn)(
            f"Qt did not shut down within {seconds:g}s; forcing exit. "
            f"Downloads already written to disk are unaffected.")

        exit_func(code)

    thread = threading.Thread(target=watchdog, name="exit-watchdog",
                              daemon=True)
    thread.start()

    return thread


def _warn(message):
    """Print a shutdown warning to stderr.

    :param message: Text to print
    :type message: str
    """
    print(message, file=sys.stderr, flush=True)


def shutdown_qt(app, window):
    """Release a Qt application and its main window in the right order.

    QtWebEngine only lets go of its render process once the view and the
    page are destroyed, and destroying them has to happen while the
    QApplication is still alive.

    :param app: The running application
    :type app: QtWidgets.QApplication

    :param window: The main window
    :type window: QtWidgets.QMainWindow or None
    """
    if window is not None:
        shutdown = getattr(window, "shutdown", None)
        if callable(shutdown):
            shutdown()

        window.deleteLater()

    # Let the deferred deletions actually run before the application goes.
    app.processEvents()
    app.sendPostedEvents(None, 0)
    app.processEvents()
