"""Tests for finding the files shipped with the application.

Running from source these sit next to the sources; in a frozen build they are
unpacked somewhere else. Getting this wrong means the animation list is not
found and the 'All animations' mode fails on exactly the builds that are
hardest to debug.
"""

# Stdlib modules
import os
import sys

# Local modules
from mixamo import resources
from mixamo.resources import SOURCE_DIR, resource_dirs, resource_path


class TestRunningFromSource:
    """The plain case: no freezing involved."""

    def test_looks_next_to_the_sources(self):
        assert SOURCE_DIR in resource_dirs()

    def test_finds_the_shipped_animation_list(self):
        found = resource_path("mixamo_anims.json")

        assert os.path.exists(found)
        assert os.path.dirname(found) == SOURCE_DIR

    def test_falls_back_to_the_source_tree_for_a_missing_file(self):
        # The caller then reports a path a human recognises, rather than one
        # inside a temporary unpack folder.
        missing = resource_path("not-shipped.json")

        assert missing == os.path.join(SOURCE_DIR, "not-shipped.json")


class TestFrozenBuild:
    """PyInstaller and friends unpack data files elsewhere."""

    def test_prefers_the_unpacked_bundle(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

        bundled = tmp_path / "mixamo_anims.json"
        bundled.write_text("{}")

        assert resource_path("mixamo_anims.json") == str(bundled)

    def test_bundle_comes_before_the_source_tree(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

        directories = resource_dirs()

        assert directories.index(str(tmp_path)) < directories.index(SOURCE_DIR)

    def test_also_looks_beside_the_executable(self, tmp_path, monkeypatch):
        # Covers a one-folder build, which ships its data unpacked rather
        # than inside the executable.
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        monkeypatch.setattr(sys, "executable",
                            str(tmp_path / "mixamo_downloader"))

        shipped = tmp_path / "mixamo_anims.json"
        shipped.write_text("{}")

        assert resource_path("mixamo_anims.json") == str(shipped)

    def test_still_finds_files_when_the_bundle_lacks_them(self, tmp_path,
                                                          monkeypatch):
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

        # Nothing in the bundle: the source tree copy must still be found.
        found = resource_path("mixamo_anims.json")

        assert os.path.dirname(found) == SOURCE_DIR
        assert os.path.exists(found)


class TestJobDefault:
    """The animation list the 'All animations' mode reads."""

    def test_points_at_a_file_that_exists(self):
        from mixamo.job import ANIMS_FILE

        assert os.path.exists(ANIMS_FILE)

    def test_is_resolved_through_the_resource_lookup(self):
        from mixamo.job import ANIMS_FILE

        assert ANIMS_FILE == resources.resource_path("mixamo_anims.json")
