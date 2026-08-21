"""Tests for shutting the application down.

Closing the window has to end the process. Two things get in the way of Qt's
'quit when the last window closes': a system tray icon is backed by a
top-level window on some desktops, and QtWebEngine keeps a render process
alive for as long as its view and page exist. Both are torn down explicitly,
and these tests hold that in place.

The window is built on the offscreen platform with the page load stubbed out,
so nothing here needs a display or the network.
"""

# Stdlib modules
import os

# Third-party modules
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qt_app():
    """A QApplication for the whole module, or a skip if Qt cannot start."""
    try:
        from PySide6 import QtWidgets
    except ImportError:  # pragma: no cover - PySide6 is a hard dependency
        pytest.skip("PySide6 is not available")

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])

    yield app


@pytest.fixture
def window(qt_app, monkeypatch):
    """A main window whose browser never leaves the machine."""
    import webpage

    # Loading mixamo.com would make the suite need a network.
    monkeypatch.setattr(webpage.CustomWebPage, "setUrl",
                        lambda self, url: None)

    from ui import MixamoDownloaderUI

    made = MixamoDownloaderUI()
    yield made

    if made.browser is not None or made.page is not None:
        made.shutdown()


class FakeTray:
    """Stands in for the tray icon, which offscreen Qt does not provide."""

    def __init__(self):
        self.hidden = False
        self.deleted = False

    def hide(self):
        self.hidden = True

    def deleteLater(self):
        self.deleted = True


class FakeThread:
    """Stands in for the worker's QThread."""

    def __init__(self, finishes=True):
        self.finishes = finishes
        self.quit_called = False
        self.waits = []

    def quit(self):
        self.quit_called = True

    def wait(self, milliseconds):
        self.waits.append(milliseconds)
        return self.finishes


class FakeWorker:
    """Stands in for the download worker."""

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class TestShutdown:
    """Everything that can outlive the window has to be released."""

    def test_hides_and_deletes_the_tray_icon(self, window):
        # A visible tray icon counts as a top-level window on some desktops,
        # which stops Qt from quitting when the main window closes.
        tray = FakeTray()
        window.tray = tray

        window.shutdown()

        assert tray.hidden
        assert tray.deleted
        assert window.tray is None

    def test_releases_the_browser_and_its_page(self, window):
        assert window.browser is not None
        assert window.page is not None

        window.shutdown()

        # The render process only exits once both of these are gone.
        assert window.browser is None
        assert window.page is None

    def test_is_safe_to_call_twice(self, window):
        window.shutdown()
        window.shutdown()

        assert window.browser is None

    def test_asking_for_a_token_after_shutdown_does_nothing(self, window):
        window.shutdown()

        # A queued token request must not resurrect a deleted page.
        window.request_token()


class TestCloseEvent:
    """Closing the window is the path users actually take."""

    def test_closing_an_idle_window_shuts_everything_down(self, window):
        from PySide6 import QtGui

        event = QtGui.QCloseEvent()
        window.closeEvent(event)

        assert event.isAccepted()
        assert window.browser is None
        assert window.page is None

    def test_closing_stores_the_settings(self, window):
        from PySide6 import QtGui

        window.le_path.setText("/tmp/mixamo-close-test")
        window.closeEvent(QtGui.QCloseEvent())

        assert window.settings.value("output_path", "", str) == \
            "/tmp/mixamo-close-test"


class TestStopWorker:
    """A download in progress must not hold the shutdown open forever."""

    def test_stops_the_worker_and_waits_for_the_thread(self, window):
        window.worker = FakeWorker()
        window.thread = FakeThread(finishes=True)

        window.stop_worker()

        assert window.worker.stopped
        assert window.thread.quit_called
        assert window.thread.waits == [5000]

    def test_waits_longer_when_the_thread_does_not_come_back(self, window):
        window.worker = FakeWorker()
        window.thread = FakeThread(finishes=False)

        window.stop_worker()

        # A socket stuck in a read can take until its timeout; the second
        # wait covers that instead of abandoning a running thread.
        assert window.thread.waits == [5000, 60000]

    def test_does_nothing_without_a_running_download(self, window):
        window.worker = None
        window.thread = None

        window.stop_worker()
