"""Tests for the Qt bridge between the UI and the download core.

These exercise the worker's wiring only: no window, no browser and no event
loop are created, so the suite still runs headless.
"""

# Stdlib modules
import threading

# Third-party modules
import pytest

# Local modules
import downloader as downloader_module
from downloader import MixamoDownloader
from mixamo.client import default_preferences
from mixamo.errors import TokenUnavailable
from mixamo.job import JobResult
from webpage import TOKEN_MARKER, extract_token


class TestTokenParsing:
    """Reading the token out of a console line."""

    def test_reads_the_token_behind_the_marker(self):
        assert extract_token(f"{TOKEN_MARKER}eyJhbGciOi") == "eyJhbGciOi"

    def test_reports_an_empty_token_when_the_user_is_logged_out(self):
        assert extract_token(TOKEN_MARKER) == ""

    def test_still_understands_the_old_marker(self):
        assert extract_token("ACCESS TOKEN: abc123") == "abc123"

    @pytest.mark.parametrize("message", [
        "", None, "Failed to load resource: 404", "Mixed content warning",
    ])
    def test_leaves_ordinary_console_lines_alone(self, message):
        assert extract_token(message) is None


class TestWorker:
    """The worker owns the stop flag and the token the client uses."""

    def test_stop_is_immediate_and_shared_with_the_client(self):
        worker = MixamoDownloader("/tmp/out", "all")

        assert not worker.stopped

        worker.stop()

        assert worker.stopped
        # The client blocks on this same event during sleeps and polls, which
        # is what makes Stop take effect mid-download.
        assert worker.client.stop is worker.stop_event

    def test_the_starting_token_is_handed_to_the_client(self):
        worker = MixamoDownloader("/tmp/out", "all", token="first-token")

        assert worker.tokens.get() == "first-token"

    def test_a_refreshed_token_replaces_the_old_one(self):
        worker = MixamoDownloader("/tmp/out", "all", token="stale")
        worker.set_token("fresh")

        assert worker.tokens.get() == "fresh"

    def test_asking_for_a_token_reaches_the_ui_as_a_signal(self):
        worker = MixamoDownloader("/tmp/out", "all")
        asked = []
        worker.token_needed.connect(lambda: asked.append(True))

        # No token and no answer: the provider gives up shortly after asking.
        worker.tokens.wait_timeout = 0.05

        with pytest.raises(TokenUnavailable):
            worker.tokens.get()

        assert asked == [True]

    def test_run_emits_the_result(self, monkeypatch):
        result = JobResult(downloaded=3)
        monkeypatch.setattr(downloader_module, "DownloadJob",
                            lambda *a, **k: type("J", (), {
                                "run": lambda self: result})())

        worker = MixamoDownloader("/tmp/out", "all", token="t")
        received = []
        worker.finished.connect(received.append)

        worker.run()

        assert received == [result]

    def test_run_still_finishes_when_the_job_explodes(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(downloader_module, "DownloadJob", boom)

        worker = MixamoDownloader("/tmp/out", "all", token="t")
        received = []
        errors = []
        worker.finished.connect(received.append)
        worker.log.connect(lambda level, text: errors.append((level, text)))

        worker.run()

        # Without this the UI would sit with a disabled Start button forever.
        assert received == [None]
        assert errors and errors[0][0] == "error"

    def test_job_events_are_forwarded_as_signals(self, monkeypatch):
        seen = {}

        def fake_job(client, path, mode, query=None, resume=True, events=None,
                     stop=None):
            seen["events"] = events
            return type("J", (), {"run": lambda self: JobResult()})()

        monkeypatch.setattr(downloader_module, "DownloadJob", fake_job)

        worker = MixamoDownloader("/tmp/out", "all", token="t")
        logs, totals, steps, items = [], [], [], []
        worker.log.connect(lambda level, text: logs.append((level, text)))
        worker.total_tasks.connect(totals.append)
        worker.current_task.connect(steps.append)
        worker.current_item.connect(items.append)

        worker.run()

        events = seen["events"]
        events.message("info", "hello")
        events.total(7)
        events.progress(3, 7)
        events.item_started("Walking")

        assert logs[-1] == ("info", "hello")
        assert totals == [7]
        assert steps == [3]
        assert items == ["Walking"]


class TestWorkerPreferences:
    """The worker is the only thing standing between the UI and the API."""

    def test_defaults_when_the_ui_passes_nothing(self):
        worker = MixamoDownloader("/tmp/out", "all")

        assert worker.preferences == default_preferences()
        assert worker.client.fps == "30"
        assert worker.client.skin == "false"

    def test_selected_options_reach_the_client(self):
        worker = MixamoDownloader("/tmp/out", "all", preferences={
            "fps": "60", "skin": "true", "reduce_kf": "1"})

        assert worker.client.fps == "60"
        assert worker.client.skin == "true"
        assert worker.client.reduce_kf == "1"

    def test_a_value_the_api_would_reject_never_leaves_the_worker(self):
        worker = MixamoDownloader("/tmp/out", "all", preferences={
            "fps": "13", "export_format": "not-a-format", "junk": 1})

        assert worker.client.fps == "30"
        assert worker.client.export_format == "fbx7_2019"
        assert not hasattr(worker.client, "junk")
