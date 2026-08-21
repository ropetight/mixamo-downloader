"""Main window of the Mixamo Downloader."""

# Stdlib modules
import html
import os
import time

# Third-party modules
from PySide6 import QtCore, QtGui, QtWebEngineWidgets, QtWidgets

# Local modules
from downloader import MixamoDownloader
from webpage import CustomWebPage


# How long to wait for the embedded browser to hand over an access token
# before telling the user something is wrong with their session.
TOKEN_TIMEOUT_MS = 15000

# Colours used by the log panel, per level.
LOG_COLOURS = {
    "debug": "#888888",
    "info": "#c8c8c8",
    "success": "#5db85d",
    "warning": "#e0a030",
    "error": "#e05c5c",
}


class MixamoDownloaderUI(QtWidgets.QMainWindow):
    """UI that allows users to bulk download animations from Mixamo.

    Users should log into their Mixamo accounts and upload the character
    they want to download animations for. This character is what Mixamo
    call the Primary Character.

    Users can choose to download all animations in Mixamo (quite slow),
    only those that contain a specific word (faster), or just the T-Pose.

    Note that only the T-Pose is downloaded with skin. Animations are
    downloaded without skin to speed things up and save space on disk.

    A download can be stopped at any point and resumed later: whatever has
    already been written to the output folder is skipped on the next run.
    """

    def __init__(self):
        """Initialize the Mixamo Downloader UI."""
        super().__init__()

        self.setWindowTitle("Mixamo Downloader")
        self.setGeometry(100, 100, 1200, 900)

        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "mixamo.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QtGui.QIcon(icon_path))

        self.settings = QtCore.QSettings("MixamoDownloader",
                                         "MixamoDownloader")

        # Worker state. Both are None while no download is running.
        self.thread = None
        self.worker = None
        # Tells the token handler whether a token is wanted to start a new
        # download or to refresh the one a running download is using.
        self._awaiting_start = False
        self._failures = []

        self._build_browser()
        self._build_widgets()
        self._restore_settings()

        self.status("Log into Mixamo and pick your character to begin.")

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_browser(self):
        """Create the embedded browser and its custom page."""
        self.browser = QtWebEngineWidgets.QWebEngineView()

        # Keep a reference to the page: setPage does not take ownership, and
        # a garbage collected page takes the whole render process with it.
        self.page = CustomWebPage(self.browser)
        self.browser.setPage(self.page)
        self.page.setUrl(QtCore.QUrl("https://www.mixamo.com"))

        self.page.retrieved_token.connect(self.on_token)
        self.page.console_message.connect(self.on_console_message)

    def _build_widgets(self):
        """Build every widget below the browser."""
        central_widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central_widget)
        layout.setSpacing(10)

        layout.addWidget(self.browser, stretch=1)
        layout.addWidget(self._build_options())
        layout.addWidget(self._build_output())
        layout.addLayout(self._build_actions())
        layout.addWidget(self._build_progress())
        layout.addWidget(self._build_log())

        self.setCentralWidget(central_widget)
        self.setStatusBar(QtWidgets.QStatusBar())

        self.tray = None
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QtWidgets.QSystemTrayIcon(self.windowIcon(), self)
            self.tray.setToolTip("Mixamo Downloader")
            self.tray.show()

    def _build_options(self):
        """Build the download mode options.

        :rtype: QtWidgets.QWidget
        """
        box = QtWidgets.QGroupBox("What to download")
        layout = QtWidgets.QHBoxLayout(box)

        self.rb_all = QtWidgets.QRadioButton("All animations")
        self.rb_all.setChecked(True)
        self.rb_query = QtWidgets.QRadioButton("Animations containing:")
        self.rb_tpose = QtWidgets.QRadioButton("T-Pose (with skin)")

        self.le_query = QtWidgets.QLineEdit()
        self.le_query.setEnabled(False)
        self.le_query.setPlaceholderText("walk, jump, zombie...")
        self.le_query.returnPressed.connect(self.start_download)

        # Only the query mode needs the keyword field.
        self.rb_query.toggled.connect(self.le_query.setEnabled)

        self.cb_resume = QtWidgets.QCheckBox("Resume (skip files already "
                                             "downloaded)")
        self.cb_resume.setChecked(True)
        self.cb_resume.setToolTip(
            "Animations recorded in the output folder's manifest, or already "
            "present there as FBX files, are skipped.")

        for widget in (self.rb_all, self.rb_query, self.le_query,
                       self.rb_tpose):
            layout.addWidget(widget)

        layout.addStretch(1)
        layout.addWidget(self.cb_resume)

        return box

    def _build_output(self):
        """Build the output folder picker.

        :rtype: QtWidgets.QWidget
        """
        box = QtWidgets.QGroupBox("Output folder")
        layout = QtWidgets.QHBoxLayout(box)

        self.le_path = QtWidgets.QLineEdit()
        self.le_path.setPlaceholderText(
            "Leave empty to download next to the application.")

        browse = QtWidgets.QToolButton()
        browse.setIcon(self.style().standardIcon(
            QtWidgets.QStyle.StandardPixmap.SP_DirIcon))
        browse.setToolTip("Choose the output folder")
        browse.clicked.connect(self.set_path)

        reveal = QtWidgets.QToolButton()
        reveal.setIcon(self.style().standardIcon(
            QtWidgets.QStyle.StandardPixmap.SP_DirOpenIcon))
        reveal.setToolTip("Open the output folder")
        reveal.clicked.connect(self.open_path)

        layout.addWidget(self.le_path)
        layout.addWidget(browse)
        layout.addWidget(reveal)

        return box

    def _build_actions(self):
        """Build the Start/Stop buttons.

        :rtype: QtWidgets.QHBoxLayout
        """
        layout = QtWidgets.QHBoxLayout()

        self.get_btn = QtWidgets.QPushButton("Start download")
        self.get_btn.setMinimumHeight(34)
        self.get_btn.clicked.connect(self.start_download)

        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setMinimumHeight(34)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_download)

        layout.addWidget(self.get_btn, stretch=2)
        layout.addWidget(self.stop_btn, stretch=1)

        return layout

    def _build_progress(self):
        """Build the progress bar and the current item label.

        :rtype: QtWidgets.QWidget
        """
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFormat("%v / %m  (%p%)")
        self.progress_bar.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)

        self.lbl_item = QtWidgets.QLabel("Idle.")
        self.lbl_item.setStyleSheet("color: #888888;")

        layout.addWidget(self.progress_bar)
        layout.addWidget(self.lbl_item)

        return widget

    def _build_log(self):
        """Build the collapsible log panel.

        :rtype: QtWidgets.QWidget
        """
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setMinimumHeight(120)
        self.log_view.setFont(QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.SystemFont.FixedFont))

        box = QtWidgets.QGroupBox("Log")
        box.setCheckable(True)
        box.setChecked(True)
        layout = QtWidgets.QVBoxLayout(box)
        layout.addWidget(self.log_view)

        box.toggled.connect(self.log_view.setVisible)

        return box

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _restore_settings(self):
        """Restore the options the user picked last time."""
        self.le_path.setText(self.settings.value("output_path", "", str))
        self.le_query.setText(self.settings.value("query", "", str))
        self.cb_resume.setChecked(
            self.settings.value("resume", True, bool))

        mode = self.settings.value("mode", "all", str)
        {"query": self.rb_query, "tpose": self.rb_tpose}.get(
            mode, self.rb_all).setChecked(True)

    def _store_settings(self):
        """Remember the current options for the next session."""
        self.settings.setValue("output_path", self.le_path.text())
        self.settings.setValue("query", self.le_query.text())
        self.settings.setValue("resume", self.cb_resume.isChecked())
        self.settings.setValue("mode", self.get_mode())

    # ------------------------------------------------------------------
    # Logging and notifications
    # ------------------------------------------------------------------

    def log(self, level, text):
        """Append a line to the log panel.

        :param level: One of 'debug', 'info', 'success', 'warning', 'error'
        :type level: str

        :param text: Message to show
        :type text: str
        """
        colour = LOG_COLOURS.get(level, LOG_COLOURS["info"])
        stamp = time.strftime("%H:%M:%S")

        # The message can contain anything Mixamo sent back, so escape it
        # before dropping it into the rich text panel.
        safe = html.escape(str(text))

        self.log_view.appendHtml(
            f'<span style="color:{LOG_COLOURS["debug"]}">{stamp}</span> '
            f'<span style="color:{colour}">{safe}</span>')

        if level in ("warning", "error"):
            self.status(text)

    def status(self, text):
        """Show a message in the status bar.

        :param text: Message to show
        :type text: str
        """
        bar = self.statusBar()
        if bar:
            bar.showMessage(text)

    def notify(self, title, text, level="info"):
        """Raise a desktop notification, falling back to the log panel.

        :param title: Notification title
        :type title: str

        :param text: Notification body
        :type text: str

        :param level: Severity, used to pick the notification icon
        :type level: str
        """
        self.log(level, text)

        if not self.tray:
            return

        icons = {
            "error": QtWidgets.QSystemTrayIcon.MessageIcon.Critical,
            "warning": QtWidgets.QSystemTrayIcon.MessageIcon.Warning,
        }
        icon = icons.get(level, QtWidgets.QSystemTrayIcon.MessageIcon.Information)

        self.tray.showMessage(title, text, icon, 8000)

    @QtCore.Slot(str, str)
    def on_console_message(self, level, text):
        """Mirror browser console warnings and errors into the log panel.

        :param level: Severity reported by the browser
        :type level: str

        :param text: Console message
        :type text: str
        """
        # Mixamo's own page is noisy; only surface things that matter.
        if level == "error":
            self.log("debug", f"[browser] {text}")

    # ------------------------------------------------------------------
    # Token handling
    # ------------------------------------------------------------------

    def request_token(self):
        """Ask the embedded browser for the current Mixamo access token."""
        self.page.request_token()

    @QtCore.Slot()
    def on_token_needed(self):
        """Refresh the token for a running download."""
        self.log("info", "Access token expired; asking the browser for a "
                         "fresh one...")
        self.request_token()

    @QtCore.Slot(str)
    def on_token(self, token):
        """Route a token scraped from the browser to whoever asked for it.

        :param token: Mixamo access token, empty when the user is logged out
        :type token: str
        """
        if not token:
            if self._awaiting_start:
                self._awaiting_start = False
                self._reset_controls()
                self.notify(
                    "Not logged in",
                    "No Mixamo access token found. Log into Mixamo in the "
                    "window above, then start the download again.",
                    level="error")
            else:
                self.log("error", "Mixamo returned no access token. Log in "
                                  "again in the browser above.")
            return

        if self._awaiting_start:
            self._awaiting_start = False
            self._start_worker(token)
            return

        if self.worker is not None:
            self.worker.set_token(token)
            self.log("info", "Access token refreshed.")

    # ------------------------------------------------------------------
    # Download lifecycle
    # ------------------------------------------------------------------

    @QtCore.Slot()
    def start_download(self):
        """Validate the options and kick a download off."""
        if self.thread is not None:
            self.log("warning", "A download is already running.")
            return

        if self.get_mode() == "query" and not self.le_query.text().strip():
            self.notify("Nothing to search for",
                        "Enter a word to search for, or pick "
                        "'All animations'.", level="warning")
            return

        path = self.le_path.text().strip()
        if path and not self._check_writable(path):
            return

        self._store_settings()
        self._failures = []

        self.get_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setRange(0, 0)
        self.lbl_item.setText("Reading your Mixamo session...")
        self.status("Reading your Mixamo session...")

        # The token lives in the browser, so the download can only start once
        # the page answers. Guard against a page that never does.
        self._awaiting_start = True
        QtCore.QTimer.singleShot(TOKEN_TIMEOUT_MS, self._token_timeout)
        self.request_token()

    def _token_timeout(self):
        """Give up waiting for the browser to hand over a token."""
        if not self._awaiting_start:
            return

        self._awaiting_start = False
        self._reset_controls()
        self.notify("Mixamo did not answer",
                    "The Mixamo page did not return an access token. Reload "
                    "it, make sure you are logged in, and try again.",
                    level="error")

    def _check_writable(self, path):
        """Make sure the output folder can actually be written to.

        :param path: Output folder path
        :type path: str

        :rtype: bool
        """
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            self.notify("Cannot use that folder",
                        f"{path} cannot be created: {exc}", level="error")
            return False

        if not os.access(path, os.W_OK):
            self.notify("Cannot use that folder",
                        f"{path} is not writable.", level="error")
            return False

        return True

    def _start_worker(self, token):
        """Move a fresh downloader onto its own thread and start it.

        :param token: Mixamo access token
        :type token: str
        """
        self.thread = QtCore.QThread(self)

        self.worker = MixamoDownloader(
            self.le_path.text().strip(),
            self.get_mode(),
            query=self.le_query.text().strip(),
            resume=self.cb_resume.isChecked(),
            token=token)

        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)

        self.worker.total_tasks.connect(self.set_progress_bar)
        self.worker.current_task.connect(self.update_progress_bar)
        self.worker.current_item.connect(self.on_current_item)
        self.worker.item_failed.connect(self.on_item_failed)
        self.worker.log.connect(self.log)
        self.worker.token_needed.connect(self.on_token_needed)
        self.worker.finished.connect(self.on_finished)

        # Canonical Qt teardown: quit the loop, let the worker delete itself
        # inside its own thread, then drop the thread.
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.on_thread_finished)

        self.progress_bar.setRange(0, 0)
        self.stop_btn.setEnabled(True)
        self.get_btn.setEnabled(False)

        self.thread.start()

    @QtCore.Slot()
    def stop_download(self):
        """Ask the running download to stop.

        The worker checks for this between chunks of the file it is currently
        downloading, so it takes effect within a second rather than at the end
        of the animation.
        """
        if self.worker is None:
            return

        self.stop_btn.setEnabled(False)
        self.lbl_item.setText("Stopping...")
        self.status("Stopping...")
        self.worker.stop()

    @QtCore.Slot(object)
    def on_finished(self, result):
        """Report the outcome of a finished run.

        :param result: Summary of the run, or None if the worker crashed
        :type result: mixamo.job.JobResult or None
        """
        if result is None:
            self.notify("Download failed",
                        "The download stopped because of an unexpected "
                        "error. See the log for details.", level="error")
            return

        level = "error" if (result.error or result.aborted) else (
            "warning" if (result.stopped or result.failed) else "success")

        title = {"error": "Download failed",
                 "warning": "Download stopped",
                 "success": "Download complete"}[level]

        self.notify(title, result.summary(), level=level)

        if result.failures:
            self.log("info", "Failed animations are kept in the folder's "
                             "manifest and retried on the next run.")

    @QtCore.Slot()
    def on_thread_finished(self):
        """Clean up once the worker thread has actually exited."""
        self.worker = None
        self.thread = None

        self._reset_controls()

    def _reset_controls(self):
        """Put the buttons and progress bar back to their idle state."""
        self.get_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setRange(0, 1)
        self.lbl_item.setText("Idle.")

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    @QtCore.Slot(int)
    def set_progress_bar(self, total_tasks):
        """Set the progress bar range to the number of animations to download.

        :param total_tasks: Number of animations this run will download
        :type total_tasks: int
        """
        self.progress_bar.setRange(0, max(1, total_tasks))
        self.progress_bar.setValue(0)

    @QtCore.Slot(int)
    def update_progress_bar(self, step):
        """Advance the progress bar.

        :param step: Number of animations downloaded so far
        :type step: int
        """
        self.progress_bar.setValue(step)

    @QtCore.Slot(str)
    def on_current_item(self, name):
        """Show which animation is being downloaded.

        :param name: Animation description
        :type name: str
        """
        self.lbl_item.setText(f"Downloading: {name}")

    @QtCore.Slot(str, str)
    def on_item_failed(self, name, reason):
        """Keep track of a failed animation.

        :param name: Animation description
        :type name: str

        :param reason: Why it failed
        :type reason: str
        """
        self._failures.append((name, reason))

    # ------------------------------------------------------------------
    # Output folder
    # ------------------------------------------------------------------

    @QtCore.Slot()
    def set_path(self):
        """Ask the user to select the output folder through a QFileDialog."""
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select the output folder", self.le_path.text())

        if path:
            self.le_path.setText(path)
            self._store_settings()

    @QtCore.Slot()
    def open_path(self):
        """Open the output folder in the system file manager."""
        path = self.le_path.text().strip() or os.getcwd()

        if not os.path.isdir(path):
            self.log("warning", f"{path} does not exist yet.")
            return

        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def get_mode(self):
        """Read the radio buttons to know which download mode to use.

        :return: Download mode ("all", "query" or "tpose")
        :rtype: str
        """
        if self.rb_query.isChecked():
            return "query"
        if self.rb_tpose.isChecked():
            return "tpose"
        return "all"

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        """Stop a running download before letting the window close.

        :param event: Close event
        :type event: QtGui.QCloseEvent
        """
        self._store_settings()

        if self.thread is None:
            event.accept()
            return

        answer = QtWidgets.QMessageBox.question(
            self, "Download in progress",
            "A download is still running. Stop it and quit?",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No)

        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        self.worker.stop()
        self.thread.quit()
        # The worker wakes up within a second, so this wait is short. Anything
        # already written stays on disk and is skipped on the next run.
        self.thread.wait(10000)

        event.accept()
