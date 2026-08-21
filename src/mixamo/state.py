"""Resume support.

A manifest kept next to the downloaded FBX files records which animations
already made it to disk and which ones failed, so a run that dies half-way
(expired token, dropped connection, Stop button) picks up where it left off
instead of starting the 2346-animation list from scratch.
"""

# Stdlib modules
import json
import os
import tempfile
import time


FILENAME = ".mixamo_downloader.json"
VERSION = 1


class DownloadState:
    """Per-character record of what has already been downloaded."""

    def __init__(self, directory, character_id, data=None, clock=time.time):
        """Initialize the state.

        :param directory: Output folder holding the FBX files
        :type directory: str

        :param character_id: Primary character the files belong to
        :type character_id: str

        :param data: Raw manifest contents (used by :meth:`load`)
        :type data: dict or None

        :param clock: Time source (injected so tests stay deterministic)
        :type clock: callable
        """
        self.directory = directory or "."
        self.character_id = character_id
        self.clock = clock

        self._data = data if isinstance(data, dict) else {}
        self._data.setdefault("version", VERSION)
        self._data.setdefault("characters", {})

        # Entries are namespaced by character: the same folder can legitimately
        # hold runs for more than one rig, and a 'Walking.fbx' exported for
        # one character says nothing about another.
        self._entry = self._data["characters"].setdefault(
            character_id, {"completed": {}, "failed": {}})
        self._entry.setdefault("completed", {})
        self._entry.setdefault("failed", {})

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @property
    def path(self):
        """Full path of the manifest file on disk."""
        return os.path.join(self.directory, FILENAME)

    @classmethod
    def load(cls, directory, character_id, clock=time.time):
        """Read the manifest from a folder, tolerating a missing or bad file.

        :param directory: Output folder
        :type directory: str

        :param character_id: Primary character ID
        :type character_id: str

        :return: Loaded state
        :rtype: DownloadState
        """
        path = os.path.join(directory or ".", FILENAME)

        data = None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            # A corrupt manifest must not block the download; the on-disk
            # FBX scan in `pending` rebuilds most of it anyway.
            data = None

        return cls(directory, character_id, data=data, clock=clock)

    def save(self):
        """Write the manifest atomically so a crash cannot corrupt it."""
        self._data["updated_at"] = self.clock()

        try:
            os.makedirs(self.directory, exist_ok=True)

            handle, tmp_path = tempfile.mkstemp(
                dir=self.directory, prefix=".mixamo-state-", suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as tmp_file:
                json.dump(self._data, tmp_file, indent=1)

            os.replace(tmp_path, self.path)
        except OSError:
            # Losing the manifest costs us resume information, not the run.
            return False

        return True

    # ------------------------------------------------------------------
    # Queries and updates
    # ------------------------------------------------------------------

    @property
    def completed(self):
        """Mapping of completed animation ID to file name."""
        return self._entry["completed"]

    @property
    def failed(self):
        """Mapping of failed animation ID to {'name': ..., 'reason': ...}."""
        return self._entry["failed"]

    def is_done(self, anim_id):
        """Whether an animation has already been downloaded.

        :param anim_id: Animation ID
        :type anim_id: str

        :rtype: bool
        """
        return anim_id in self._entry["completed"]

    def mark_done(self, anim_id, filename):
        """Record an animation as downloaded.

        :param anim_id: Animation ID
        :type anim_id: str

        :param filename: File name written to disk
        :type filename: str
        """
        self._entry["completed"][anim_id] = filename
        self._entry["failed"].pop(anim_id, None)

    def mark_failed(self, anim_id, name, reason):
        """Record an animation as failed, keeping the reason for the UI.

        :param anim_id: Animation ID
        :type anim_id: str

        :param name: Animation description
        :type name: str

        :param reason: Human readable failure reason
        :type reason: str
        """
        self._entry["failed"][anim_id] = {"name": name, "reason": str(reason)}

    def forget(self, anim_id):
        """Drop every record of an animation, so it is downloaded again.

        :param anim_id: Animation ID
        :type anim_id: str
        """
        self._entry["completed"].pop(anim_id, None)
        self._entry["failed"].pop(anim_id, None)

    def adopt_existing_files(self, animations, extension=".fbx"):
        """Mark animations whose exported file is already on disk as completed.

        This is what makes resume work for downloads made before the manifest
        existed, or after someone moved files around by hand.

        :param animations: Mapping of animation ID to description
        :type animations: dict

        :param extension: Extension the selected export format produces
        :type extension: str

        :return: Number of animations adopted
        :rtype: int
        """
        from .client import safe_filename

        adopted = 0

        for anim_id, name in animations.items():
            if self.is_done(anim_id):
                continue

            candidate = os.path.join(
                self.directory, f"{safe_filename(name)}{extension}")

            try:
                # A zero byte file is a failed download, not a finished one.
                if os.path.getsize(candidate) > 0:
                    self.mark_done(anim_id, os.path.basename(candidate))
                    adopted += 1
            except OSError:
                continue

        return adopted

    def pending(self, animations, resume=True, extension=".fbx"):
        """Work out which animations still need downloading.

        :param animations: Mapping of animation ID to description
        :type animations: dict

        :param resume: Skip animations already recorded or present on disk
        :type resume: bool

        :param extension: Extension the selected export format produces
        :type extension: str

        :return: (list of (anim_id, name) still to do, number skipped)
        :rtype: tuple
        """
        if not resume:
            return list(animations.items()), 0

        self.adopt_existing_files(animations, extension=extension)

        todo = [(anim_id, name) for anim_id, name in animations.items()
                if not self.is_done(anim_id)]

        return todo, len(animations) - len(todo)
