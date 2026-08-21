"""Tests for the download orchestration."""

# Stdlib modules
import json
import os
import threading

# Third-party modules
import pytest

# Local modules
from mixamo.errors import (AuthError, ExportFailed, MixamoError, Stopped,
                           TransientError)
from mixamo.job import MAX_CONSECUTIVE_FAILURES, DownloadJob, JobEvents
from mixamo.state import DownloadState


class RecordingEvents(JobEvents):
    """Event sink that keeps everything for later inspection."""

    def __init__(self):
        self.messages = []
        self.totals = []
        self.progress_updates = []
        self.started = []
        self.done = []
        self.failures = []

    def message(self, level, text):
        self.messages.append((level, text))

    def total(self, count):
        self.totals.append(count)

    def progress(self, done, total):
        self.progress_updates.append((done, total))

    def item_started(self, name):
        self.started.append(name)

    def item_done(self, name):
        self.done.append(name)

    def item_failed(self, name, reason):
        self.failures.append((name, reason))

    @property
    def text(self):
        """All message bodies joined, for coarse assertions."""
        return " | ".join(text for _, text in self.messages)


class FakeClient:
    """Stand-in for MixamoClient driven by per-animation behaviour."""

    extension = ".fbx"

    def __init__(self, behaviour=None, character=("char-1", "Victoria"),
                 search=None, stop=None):
        """Initialize the fake client.

        :param behaviour: Mapping of animation ID to an exception (raised) or
            a callable invoked instead of the download
        :type behaviour: dict or None

        :param character: (id, name) returned by :meth:`primary_character`
        :type character: tuple

        :param search: Result of :meth:`search_animations`
        :type search: dict or None
        """
        self.behaviour = behaviour or {}
        self.character = character
        self.search = search or {}
        self.stop = stop or threading.Event()

        self.downloaded = []
        self.attempts = {}

    def primary_character(self):
        if isinstance(self.character, BaseException):
            raise self.character
        return self.character

    def search_animations(self, query, limit=96):
        if isinstance(self.search, BaseException):
            raise self.search
        return self.search

    def animation_payload(self, character_id, anim_id):
        return "{}", f"anim-{anim_id}"

    def tpose_payload(self, character_id, character_name):
        return "{}"

    def export(self, character_id, payload):
        return "https://cdn/file.fbx"

    def download(self, url, dest_path, chunk_size=65536):
        anim_id = os.path.basename(dest_path)
        # The fake keys behaviour off the animation ID, which the job turns
        # into 'anim-<id>.fbx' through animation_payload.
        key = anim_id[len("anim-"):-len(".fbx")] if anim_id.startswith(
            "anim-") else anim_id

        self.attempts[key] = self.attempts.get(key, 0) + 1

        action = self.behaviour.get(key)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            action(self.attempts[key])

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as handle:
            handle.write(b"FBX")

        self.downloaded.append(key)
        return 3


def make_job(client, tmp_path, anims, events=None, stop=None, **kwargs):
    """Build a job whose animation list is the given mapping.

    :rtype: DownloadJob
    """
    anims_file = tmp_path / "anims.json"
    anims_file.write_text(json.dumps(anims))

    return DownloadJob(
        client,
        str(tmp_path / "out"),
        kwargs.pop("mode", "all"),
        events=events,
        stop=stop if stop is not None else client.stop,
        anims_file=str(anims_file),
        **kwargs)


ANIMS = {"a": "Walking", "b": "Running", "c": "Jumping"}


class TestHappyPath:
    """A clean run downloads everything and says so."""

    def test_downloads_every_animation(self, tmp_path):
        client = FakeClient()
        events = RecordingEvents()

        result = make_job(client, tmp_path, ANIMS, events).run()

        assert client.downloaded == ["a", "b", "c"]
        assert result.downloaded == 3
        assert result.failed == 0
        assert result.ok

    def test_reports_progress_for_every_item(self, tmp_path):
        events = RecordingEvents()

        make_job(FakeClient(), tmp_path, ANIMS, events).run()

        assert events.totals == [3]
        assert events.progress_updates == [(1, 3), (2, 3), (3, 3)]
        assert events.started == ["Walking", "Running", "Jumping"]
        assert events.done == ["Walking", "Running", "Jumping"]

    def test_writes_the_files_into_the_output_folder(self, tmp_path):
        make_job(FakeClient(), tmp_path, ANIMS).run()

        written = sorted(name for name in os.listdir(tmp_path / "out")
                         if name.endswith(".fbx"))

        assert written == ["anim-a.fbx", "anim-b.fbx", "anim-c.fbx"]

    def test_creates_a_manifest_for_the_next_run(self, tmp_path):
        make_job(FakeClient(), tmp_path, ANIMS).run()

        state = DownloadState.load(str(tmp_path / "out"), "char-1")

        assert sorted(state.completed) == ["a", "b", "c"]


class TestResume:
    """A second run must not redo work the first one finished."""

    def test_skips_animations_from_a_previous_run(self, tmp_path):
        make_job(FakeClient(), tmp_path, ANIMS).run()

        client = FakeClient()
        events = RecordingEvents()
        result = make_job(client, tmp_path, ANIMS, events).run()

        assert client.downloaded == []
        assert result.skipped == 3
        assert "Nothing left to download" in events.text

    def test_downloads_only_what_is_missing(self, tmp_path):
        first = FakeClient(behaviour={"b": TransientError("boom"),
                                      "c": TransientError("boom")})
        make_job(first, tmp_path, ANIMS, item_retries=0).run()

        second = FakeClient()
        events = RecordingEvents()
        result = make_job(second, tmp_path, ANIMS, events).run()

        assert second.downloaded == ["b", "c"]
        assert result.skipped == 1
        assert "Resuming" in events.text

    def test_resume_off_downloads_everything_again(self, tmp_path):
        make_job(FakeClient(), tmp_path, ANIMS).run()

        client = FakeClient()
        result = make_job(client, tmp_path, ANIMS, resume=False).run()

        assert client.downloaded == ["a", "b", "c"]
        assert result.skipped == 0

    def test_resumes_after_a_stopped_run(self, tmp_path):
        stop = threading.Event()
        client = FakeClient(
            behaviour={"b": lambda attempt: stop.set()}, stop=stop)

        first = make_job(client, tmp_path, ANIMS, stop=stop).run()

        assert first.stopped
        assert first.downloaded == 2

        resumed = FakeClient()
        second = make_job(resumed, tmp_path, ANIMS).run()

        assert resumed.downloaded == ["c"]
        assert second.ok


class TestFailureHandling:
    """One bad animation must not take the batch down with it."""

    def test_keeps_going_after_a_failure(self, tmp_path):
        client = FakeClient(behaviour={"b": ExportFailed("rig mismatch")})
        events = RecordingEvents()

        result = make_job(client, tmp_path, ANIMS, events,
                          item_retries=0).run()

        assert client.downloaded == ["a", "c"]
        assert result.downloaded == 2
        assert result.failed == 1
        assert result.failures == [("Running", "rig mismatch")]
        assert events.failures == [("Running", "rig mismatch")]

    def test_records_the_failure_for_the_next_run(self, tmp_path):
        client = FakeClient(behaviour={"b": ExportFailed("rig mismatch")})
        make_job(client, tmp_path, ANIMS, item_retries=0).run()

        state = DownloadState.load(str(tmp_path / "out"), "char-1")

        assert state.failed["b"]["reason"] == "rig mismatch"
        assert not state.is_done("b")

    def test_retries_a_transient_failure_once(self, tmp_path):
        def fail_first(attempt):
            if attempt == 1:
                raise TransientError("connection reset")

        client = FakeClient(behaviour={"b": fail_first})
        result = make_job(client, tmp_path, ANIMS, item_retries=1).run()

        assert client.attempts["b"] == 2
        assert result.downloaded == 3
        assert result.failed == 0

    def test_stops_the_run_after_too_many_failures_in_a_row(self, tmp_path):
        anims = {f"id-{index}": f"Anim {index}" for index in range(30)}
        client = FakeClient(behaviour={
            anim_id: TransientError("down") for anim_id in anims})
        events = RecordingEvents()

        result = make_job(client, tmp_path, anims, events,
                          item_retries=0).run()

        assert result.aborted
        assert result.failed == MAX_CONSECUTIVE_FAILURES
        assert "in a row" in result.error
        # The remaining 20 animations were not hammered pointlessly.
        assert len(client.attempts) == MAX_CONSECUTIVE_FAILURES

    def test_a_success_resets_the_consecutive_failure_counter(self, tmp_path):
        anims = {f"id-{index}": f"Anim {index}" for index in range(30)}
        behaviour = {anim_id: TransientError("down") for anim_id in anims}
        # Let one animation through, half way into the failure streak.
        del behaviour["id-5"]

        client = FakeClient(behaviour=behaviour)
        result = make_job(client, tmp_path, anims, item_retries=0).run()

        assert result.downloaded == 1
        assert not result.ok

    def test_an_unwritable_output_folder_is_reported(self, tmp_path):
        client = FakeClient(behaviour={"a": OSError("No space left")})

        result = make_job(client, tmp_path, ANIMS).run()

        assert result.aborted
        assert "No space left" in result.error


class TestAuthentication:
    """A dead session stops the run instead of failing 2346 animations."""

    def test_aborts_when_the_token_cannot_be_refreshed(self, tmp_path):
        client = FakeClient(behaviour={"b": AuthError("log in again")})
        events = RecordingEvents()

        result = make_job(client, tmp_path, ANIMS, events).run()

        assert result.aborted
        assert result.error == "log in again"
        assert ("error", "log in again") in events.messages
        # Whatever finished before the token died is still recorded.
        assert DownloadState.load(str(tmp_path / "out"), "char-1").is_done("a")

    def test_reports_a_missing_primary_character(self, tmp_path):
        client = FakeClient(character=MixamoError("No primary character"))

        result = make_job(client, tmp_path, ANIMS).run()

        assert result.aborted
        assert "No primary character" in result.error


class TestStopping:
    """Stop means stop, not 'after this animation'."""

    def test_stops_between_animations(self, tmp_path):
        stop = threading.Event()
        client = FakeClient(
            behaviour={"a": lambda attempt: stop.set()}, stop=stop)

        result = make_job(client, tmp_path, ANIMS, stop=stop).run()

        assert result.stopped
        assert result.downloaded == 1
        assert client.downloaded == ["a"]

    def test_stops_mid_download(self, tmp_path):
        stop = threading.Event()
        client = FakeClient(behaviour={"a": Stopped("stopped")}, stop=stop)

        result = make_job(client, tmp_path, ANIMS, stop=stop).run()

        assert result.stopped
        assert result.downloaded == 0
        assert result.failed == 0

    def test_a_stopped_run_is_not_reported_as_a_failure(self, tmp_path):
        stop = threading.Event()
        stop.set()
        client = FakeClient(stop=stop)
        events = RecordingEvents()

        result = make_job(client, tmp_path, ANIMS, events, stop=stop).run()

        assert result.stopped
        assert result.failed == 0
        assert "Stopped" in result.summary()


class TestModes:
    """Each download mode does what the radio button promises."""

    def test_query_mode_searches_mixamo(self, tmp_path):
        client = FakeClient(search={"q1": "Walk Left", "q2": "Walk Right"})

        result = make_job(client, tmp_path, ANIMS, mode="query",
                          query="walk").run()

        assert client.downloaded == ["q1", "q2"]
        assert result.total == 2

    def test_query_mode_needs_a_keyword(self, tmp_path):
        result = make_job(FakeClient(), tmp_path, ANIMS, mode="query",
                          query="   ").run()

        assert result.aborted
        assert "word to search for" in result.error

    def test_query_mode_reports_an_empty_search(self, tmp_path):
        client = FakeClient(search={})

        result = make_job(client, tmp_path, ANIMS, mode="query",
                          query="zzzz").run()

        assert result.aborted
        assert "No animations match 'zzzz'" in result.error

    def test_tpose_mode_downloads_the_character(self, tmp_path):
        client = FakeClient()
        events = RecordingEvents()

        result = make_job(client, tmp_path, ANIMS, events,
                          mode="tpose").run()

        assert result.downloaded == 1
        assert os.path.exists(tmp_path / "out" / "Victoria.fbx")
        assert events.totals == [1]


class TestSummary:
    """The summary line is what ends up in the notification."""

    def test_mentions_downloads_skips_and_failures(self, tmp_path):
        client = FakeClient(behaviour={"b": ExportFailed("nope")})
        make_job(client, tmp_path, ANIMS, item_retries=0).run()

        second = FakeClient(behaviour={"b": ExportFailed("nope")})
        result = make_job(second, tmp_path, ANIMS, item_retries=0).run()

        summary = result.summary()

        assert "already on disk" in summary
        assert "1 failed" in summary
        assert summary.startswith("Finished")
