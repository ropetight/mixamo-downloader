"""Tests for the process shutdown helpers.

The application has to end on every platform it runs on, and it has to end
*gracefully* where it can: a frozen Windows build cleans up its unpacked
temporary folder on a normal exit and leaves it behind on a forced one.
"""

# Stdlib modules
import threading

# Third-party modules
import pytest

# Local modules
from appexit import EXIT_TIMEOUT, force_exit_after, shutdown_qt


class Recorder:
    """Collects the calls a watchdog makes instead of ending the process."""

    def __init__(self):
        self.slept = []
        self.exited = []
        self.warnings = []

    def sleep(self, seconds):
        self.slept.append(seconds)

    def exit(self, code):
        self.exited.append(code)

    def warn(self, message):
        self.warnings.append(message)


class TestForceExit:
    """The watchdog is insurance, not the normal way out."""

    def test_runs_on_a_daemon_thread(self):
        # A non-daemon watchdog would itself keep the process alive, which is
        # exactly the bug it exists to prevent.
        recorder = Recorder()
        thread = force_exit_after(1, 0, sleep=recorder.sleep,
                                  exit_func=recorder.exit,
                                  warn=recorder.warn)
        thread.join(timeout=5)

        assert thread.daemon

    def test_waits_before_killing_the_process(self):
        recorder = Recorder()
        force_exit_after(7.5, 0, sleep=recorder.sleep,
                         exit_func=recorder.exit,
                         warn=recorder.warn).join(timeout=5)

        assert recorder.slept == [7.5]

    def test_kills_the_process_with_the_given_code(self):
        recorder = Recorder()
        force_exit_after(0, 3, sleep=recorder.sleep,
                         exit_func=recorder.exit,
                         warn=recorder.warn).join(timeout=5)

        assert recorder.exited == [3]

    def test_says_why_before_killing(self):
        recorder = Recorder()
        force_exit_after(0, 0, sleep=recorder.sleep,
                         exit_func=recorder.exit,
                         warn=recorder.warn).join(timeout=5)

        assert recorder.warnings
        assert "forcing exit" in recorder.warnings[0]
        # The user needs to know their downloads survived it.
        assert "Downloads already written" in recorder.warnings[0]

    def test_the_default_timeout_is_generous(self):
        # A healthy shutdown takes milliseconds; this must not fire during
        # one that is merely slow.
        assert EXIT_TIMEOUT >= 5

    def test_a_normal_exit_beats_the_watchdog(self):
        # Standing in for the interpreter finishing first: nothing is forced
        # because the thread is still sleeping when the process would end.
        started = threading.Event()
        recorder = Recorder()

        def slow_sleep(seconds):
            started.set()
            threading.Event().wait(0.2)

        force_exit_after(EXIT_TIMEOUT, 0, sleep=slow_sleep,
                         exit_func=recorder.exit, warn=recorder.warn)

        assert started.wait(timeout=5)
        assert recorder.exited == []


class FakeApp:
    """Stands in for QApplication."""

    def __init__(self):
        self.processed = 0
        self.posted = []

    def processEvents(self):
        self.processed += 1

    def sendPostedEvents(self, receiver, event_type):
        self.posted.append((receiver, event_type))


class FakeWindow:
    """Stands in for the main window."""

    def __init__(self):
        self.shutdown_called = False
        self.deleted = False

    def shutdown(self):
        self.shutdown_called = True

    def deleteLater(self):
        self.deleted = True


class TestShutdownQt:
    """Order matters: the window goes before the application does."""

    def test_shuts_the_window_down_and_schedules_its_deletion(self):
        app, window = FakeApp(), FakeWindow()

        shutdown_qt(app, window)

        assert window.shutdown_called
        assert window.deleted

    def test_drains_the_deferred_deletions(self):
        # deleteLater only takes effect once the events are delivered; miss
        # this and QtWebEngine's render process outlives the application.
        app, window = FakeApp(), FakeWindow()

        shutdown_qt(app, window)

        assert app.processed >= 2
        assert app.posted == [(None, 0)]

    def test_survives_a_window_that_is_already_gone(self):
        app = FakeApp()

        shutdown_qt(app, None)

        assert app.processed >= 1

    def test_survives_a_window_without_a_shutdown_method(self):
        app = FakeApp()

        class Bare:
            def __init__(self):
                self.deleted = False

            def deleteLater(self):
                self.deleted = True

        bare = Bare()
        shutdown_qt(app, bare)

        assert bare.deleted
