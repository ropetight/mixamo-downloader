"""Qt worker wrapping the Qt-free download core.

Everything that can fail lives in the `mixamo` package, which knows nothing
about Qt and is unit tested on its own. This module is only the bridge: it
turns core events into Qt signals and the Stop button into a threading event.
"""

# Stdlib modules
import threading

# Third-party modules
from PySide6 import QtCore

# Local modules
from mixamo.client import MixamoClient
from mixamo.job import DownloadJob, JobEvents
from mixamo.tokens import TokenProvider


class _SignalEvents(JobEvents):
    """Adapts core job events onto the worker's Qt signals."""

    def __init__(self, worker):
        """Initialize the adapter.

        :param worker: Worker whose signals should be emitted
        :type worker: MixamoDownloader
        """
        self.worker = worker

    def message(self, level, text):
        self.worker.log.emit(level, text)

    def total(self, count):
        self.worker.total_tasks.emit(count)

    def progress(self, done, total):
        self.worker.current_task.emit(done)

    def item_started(self, name):
        self.worker.current_item.emit(name)

    def item_done(self, name):
        self.worker.log.emit("success", f"Downloaded {name}")

    def item_failed(self, name, reason):
        # The warning line is already emitted by the job itself; keeping the
        # per-item signal separate lets the UI count failures without parsing.
        self.worker.item_failed.emit(name, reason)


class MixamoDownloader(QtCore.QObject):
    """Runs a bulk download on a worker thread.

    Users can download every animation in Mixamo, only those matching a
    keyword, or just the T-Pose. A run can be stopped at any time and picked
    up again later: finished animations are recorded in a manifest inside the
    output folder and skipped on the next run.
    """

    # Signals consumed by the UI.
    finished = QtCore.Signal(object)
    total_tasks = QtCore.Signal(int)
    current_task = QtCore.Signal(int)
    current_item = QtCore.Signal(str)
    item_failed = QtCore.Signal(str, str)
    log = QtCore.Signal(str, str)
    # Emitted when the worker needs the UI to scrape a fresh access token
    # out of the embedded browser.
    token_needed = QtCore.Signal()

    def __init__(self, path, mode, query=None, resume=True, token=None,
                 parent=None):
        """Initialize the downloader.

        :param path: Output folder path
        :type path: str

        :param mode: Download mode ("all", "query" or "tpose")
        :type mode: str

        :param query: Keyword to be used as query when searching animations
        :type query: str or None

        :param resume: Skip animations a previous run already downloaded
        :type resume: bool

        :param token: Access token already scraped from the browser
        :type token: str or None
        """
        super().__init__(parent)

        self.path = path
        self.mode = mode
        self.query = query
        self.resume = resume

        # A plain threading.Event, not a bool: the worker blocks on it during
        # backoff sleeps and export polls, so Stop takes effect immediately
        # rather than after the current animation finishes.
        self.stop_event = threading.Event()

        self.tokens = TokenProvider(
            request_refresh=self.token_needed.emit, stop=self.stop_event)

        if token:
            self.tokens.set(token)

        self.client = MixamoClient(self.tokens, stop=self.stop_event)
        self.result = None

    @QtCore.Slot(str)
    def set_token(self, token):
        """Hand a freshly scraped token to the worker. Called from the UI thread.

        :param token: Mixamo access token
        :type token: str
        """
        self.tokens.set(token)

    @QtCore.Slot()
    def stop(self):
        """Ask the run to stop. Called from the UI thread."""
        self.stop_event.set()

    @property
    def stopped(self):
        """Whether a stop has been requested."""
        return self.stop_event.is_set()

    @QtCore.Slot()
    def run(self):
        """Run the download and emit the result.

        Always emits :attr:`finished`, including on failure, so the UI can
        never end up with a permanently disabled Start button.
        """
        try:
            job = DownloadJob(
                self.client,
                self.path,
                self.mode,
                query=self.query,
                resume=self.resume,
                events=_SignalEvents(self),
                stop=self.stop_event)

            self.result = job.run()
        except Exception as exc:  # noqa: BLE001 - last resort safety net
            self.log.emit("error", f"Unexpected error: {exc}")
            self.result = None
        finally:
            self.finished.emit(self.result)
