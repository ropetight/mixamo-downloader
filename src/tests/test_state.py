"""Tests for the resume manifest."""

# Stdlib modules
import json
import os

# Local modules
from mixamo.state import FILENAME, DownloadState


ANIMS = {"id-1": "Walking", "id-2": "Running", "id-3": "Zombie Idle"}


def write_fbx(directory, name, content=b"FBX"):
    """Create an FBX file on disk.

    :rtype: str
    """
    path = os.path.join(str(directory), f"{name}.fbx")
    with open(path, "wb") as handle:
        handle.write(content)
    return path


class TestPersistence:
    """The manifest survives restarts and refuses to break the run."""

    def test_round_trips_through_disk(self, tmp_path):
        state = DownloadState(str(tmp_path), "char-1")
        state.mark_done("id-1", "Walking.fbx")
        state.mark_failed("id-2", "Running", "export failed")
        assert state.save()

        reloaded = DownloadState.load(str(tmp_path), "char-1")

        assert reloaded.is_done("id-1")
        assert reloaded.failed["id-2"]["reason"] == "export failed"

    def test_writes_atomically_and_leaves_no_temp_files(self, tmp_path):
        state = DownloadState(str(tmp_path), "char-1")
        state.mark_done("id-1", "Walking.fbx")
        state.save()

        leftovers = [name for name in os.listdir(tmp_path)
                     if name.endswith(".tmp")]

        assert leftovers == []
        assert os.path.exists(tmp_path / FILENAME)

    def test_a_corrupt_manifest_does_not_break_the_run(self, tmp_path):
        (tmp_path / FILENAME).write_text("{ this is not json")

        state = DownloadState.load(str(tmp_path), "char-1")

        assert state.completed == {}
        assert state.pending(ANIMS)[0] == list(ANIMS.items())

    def test_records_are_kept_per_character(self, tmp_path):
        first = DownloadState(str(tmp_path), "char-1")
        first.mark_done("id-1", "Walking.fbx")
        first.save()

        second = DownloadState.load(str(tmp_path), "char-2")

        # The same animation exported for another rig is a different file.
        assert not second.is_done("id-1")

        second.mark_done("id-2", "Running.fbx")
        second.save()

        assert DownloadState.load(str(tmp_path), "char-1").is_done("id-1")

    def test_creates_the_folder_it_writes_into(self, tmp_path):
        target = tmp_path / "does" / "not" / "exist"
        state = DownloadState(str(target), "char-1")
        state.mark_done("id-1", "Walking.fbx")

        assert state.save()
        assert (target / FILENAME).exists()


class TestPending:
    """Resume decides what is left to do."""

    def test_returns_everything_on_a_fresh_folder(self, tmp_path):
        state = DownloadState(str(tmp_path), "char-1")

        todo, skipped = state.pending(ANIMS)

        assert len(todo) == 3
        assert skipped == 0

    def test_skips_what_the_manifest_already_recorded(self, tmp_path):
        state = DownloadState(str(tmp_path), "char-1")
        state.mark_done("id-2", "Running.fbx")

        todo, skipped = state.pending(ANIMS)

        assert [anim_id for anim_id, _ in todo] == ["id-1", "id-3"]
        assert skipped == 1

    def test_adopts_files_already_on_disk(self, tmp_path):
        # Covers downloads made before the manifest existed.
        write_fbx(tmp_path, "Walking")
        state = DownloadState(str(tmp_path), "char-1")

        todo, skipped = state.pending(ANIMS)

        assert skipped == 1
        assert state.is_done("id-1")
        assert [anim_id for anim_id, _ in todo] == ["id-2", "id-3"]

    def test_ignores_empty_files_left_by_a_failed_download(self, tmp_path):
        write_fbx(tmp_path, "Walking", content=b"")
        state = DownloadState(str(tmp_path), "char-1")

        todo, skipped = state.pending(ANIMS)

        assert skipped == 0
        assert len(todo) == 3

    def test_matches_files_through_the_same_sanitizing_the_downloader_uses(
            self, tmp_path):
        write_fbx(tmp_path, "Walk_Run")
        state = DownloadState(str(tmp_path), "char-1")

        todo, _ = state.pending({"id-9": "Walk/Run"})

        assert todo == []

    def test_resume_off_returns_everything(self, tmp_path):
        write_fbx(tmp_path, "Walking")
        state = DownloadState(str(tmp_path), "char-1")
        state.mark_done("id-2", "Running.fbx")

        todo, skipped = state.pending(ANIMS, resume=False)

        assert len(todo) == 3
        assert skipped == 0


class TestBookkeeping:
    """Failures are remembered so the next run retries exactly those."""

    def test_a_previously_failed_animation_is_still_pending(self, tmp_path):
        state = DownloadState(str(tmp_path), "char-1")
        state.mark_failed("id-1", "Walking", "timeout")

        todo, _ = state.pending(ANIMS)

        assert "id-1" in [anim_id for anim_id, _ in todo]

    def test_succeeding_clears_the_earlier_failure(self, tmp_path):
        state = DownloadState(str(tmp_path), "char-1")
        state.mark_failed("id-1", "Walking", "timeout")
        state.mark_done("id-1", "Walking.fbx")

        assert state.failed == {}
        assert state.is_done("id-1")

    def test_forget_puts_an_animation_back_in_the_queue(self, tmp_path):
        state = DownloadState(str(tmp_path), "char-1")
        state.mark_done("id-1", "Walking.fbx")
        state.forget("id-1")

        assert not state.is_done("id-1")

    def test_the_manifest_is_valid_json_a_human_can_read(self, tmp_path):
        state = DownloadState(str(tmp_path), "char-1")
        state.mark_done("id-1", "Walking.fbx")
        state.save()

        data = json.loads((tmp_path / FILENAME).read_text())

        assert data["characters"]["char-1"]["completed"] == {
            "id-1": "Walking.fbx"}
