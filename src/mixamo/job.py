"""Download orchestration.

`DownloadJob` owns the run: it works out what to download, skips whatever a
previous run already finished, downloads the rest one by one, and reports
every step through an event sink. It never lets a single bad animation kill
the whole batch, and it stops promptly when asked to.
"""

# Stdlib modules
import json
import os
from dataclasses import dataclass, field

# Local modules
from .client import safe_filename
from .errors import (AuthError, ExportFailed, ExportTimeout, MixamoError,
                     RateLimited, Stopped, TokenUnavailable, TransientError)
from .state import DownloadState


# Failures worth trying once more before writing the animation off.
RETRYABLE = (TransientError, RateLimited, ExportFailed, ExportTimeout)

# Default path of the pre-scraped animation list shipped with the tool.
ANIMS_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "mixamo_anims.json")

# Give up on the whole run after this many failures in a row: at that point
# something is wrong with the account or the service, not with one animation.
MAX_CONSECUTIVE_FAILURES = 10


class JobEvents:
    """No-op event sink. Subclass it, or pass anything with these methods."""

    def message(self, level, text):
        """Report a human readable message ('info', 'warning', 'error')."""

    def total(self, count):
        """Report how many animations this run will download."""

    def progress(self, done, total):
        """Report progress after each finished animation."""

    def item_started(self, name):
        """Report which animation is being worked on."""

    def item_done(self, name):
        """Report that an animation finished downloading."""

    def item_failed(self, name, reason):
        """Report that an animation failed after its retries."""


@dataclass
class JobResult:
    """Summary of a finished (or stopped) run."""

    total: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: bool = False
    aborted: bool = False
    character_name: str = ""
    output_dir: str = ""
    error: str = ""
    failures: list = field(default_factory=list)

    @property
    def ok(self):
        """Whether the run finished with nothing outstanding."""
        return (not self.stopped and not self.aborted
                and not self.failed and not self.error)

    def summary(self):
        """One-line summary suitable for a status bar or a notification.

        :rtype: str
        """
        if self.error:
            return f"Download failed: {self.error}"

        parts = [f"{self.downloaded} downloaded"]
        if self.skipped:
            parts.append(f"{self.skipped} already on disk")
        if self.failed:
            parts.append(f"{self.failed} failed")

        prefix = "Stopped" if self.stopped else "Finished"
        return f"{prefix}: " + ", ".join(parts) + "."


class DownloadJob:
    """Runs one bulk download from start to finish."""

    def __init__(self, client, output_dir, mode, query=None, resume=True,
                 events=None, stop=None, state=None, anims_file=ANIMS_FILE,
                 item_retries=1):
        """Initialize the job.

        :param client: API client
        :type client: mixamo.client.MixamoClient

        :param output_dir: Folder the FBX files are written to
        :type output_dir: str

        :param mode: "all", "query" or "tpose"
        :type mode: str

        :param query: Keyword used by the "query" mode
        :type query: str or None

        :param resume: Skip animations a previous run already downloaded
        :type resume: bool

        :param events: Sink receiving progress and log messages
        :type events: JobEvents or None

        :param stop: Event that aborts the run once set
        :type stop: threading.Event or None

        :param state: Pre-built resume state (injected by tests)
        :type state: mixamo.state.DownloadState or None

        :param anims_file: Path of the animation list used by the "all" mode
        :type anims_file: str

        :param item_retries: Extra attempts per animation before giving up
        :type item_retries: int
        """
        self.client = client
        self.output_dir = output_dir or "."
        self.mode = mode
        self.query = query
        self.resume = resume
        self.events = events or JobEvents()
        self.stop = stop if stop is not None else client.stop
        self.state = state
        self.anims_file = anims_file
        self.item_retries = item_retries

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_all_animations(self):
        """Read the pre-scraped animation list shipped next to the sources.

        :return: Mapping of animation ID to description
        :rtype: dict
        """
        with open(self.anims_file, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _collect_animations(self):
        """Work out the full set of animations this run should download.

        :return: Mapping of animation ID to description
        :rtype: dict
        """
        if self.mode == "query":
            query = (self.query or "").strip()
            if not query:
                raise MixamoError("Enter a word to search for, or pick "
                                  "'All animations'.")

            self.events.message("info", f"Searching Mixamo for '{query}'...")
            animations = self.client.search_animations(query)

            if not animations:
                raise MixamoError(f"No animations match '{query}'.")

            return animations

        return self._load_all_animations()

    def _download_tpose(self, character_id, character_name, result):
        """Download the character's T-Pose (the only export carrying a skin).

        :param character_id: Primary character ID
        :type character_id: str

        :param character_name: Primary character name
        :type character_name: str

        :param result: Result being filled in
        :type result: JobResult
        """
        result.total = 1
        self.events.total(1)
        self.events.item_started(character_name)

        payload = self.client.tpose_payload(character_id, character_name)
        url = self.client.export(character_id, payload)

        dest = os.path.join(
            self.output_dir,
            f"{safe_filename(character_name)}{self.client.extension}")
        self.client.download(url, dest)

        result.downloaded = 1
        self.events.item_done(character_name)
        self.events.progress(1, 1)

    def _download_one(self, character_id, anim_id, name):
        """Export and download a single animation.

        :param character_id: Primary character ID
        :type character_id: str

        :param anim_id: Animation ID
        :type anim_id: str

        :param name: Animation description used for the file name
        :type name: str

        :return: File name written to disk
        :rtype: str
        """
        payload, description = self.client.animation_payload(
            character_id, anim_id)

        url = self.client.export(character_id, payload)

        filename = f"{safe_filename(description or name)}{self.client.extension}"
        self.client.download(url, os.path.join(self.output_dir, filename))

        return filename

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self):
        """Run the download.

        Never raises for an ordinary failure: everything the user needs to
        know ends up in the returned :class:`JobResult` and in the events.

        :rtype: JobResult
        """
        result = JobResult(output_dir=self.output_dir)

        try:
            return self._run(result)
        except Stopped:
            result.stopped = True
            self.events.message("warning", "Download stopped.")
            return result
        except (AuthError, TokenUnavailable) as exc:
            result.aborted = True
            result.error = str(exc)
            self.events.message("error", str(exc))
            return result
        except MixamoError as exc:
            result.aborted = True
            result.error = str(exc)
            self.events.message("error", str(exc))
            return result
        except OSError as exc:
            result.aborted = True
            result.error = f"Cannot write to {self.output_dir}: {exc}"
            self.events.message("error", result.error)
            return result

    def _run(self, result):
        """Body of :meth:`run`, wrapped there for error reporting.

        :param result: Result being filled in
        :type result: JobResult

        :rtype: JobResult
        """
        os.makedirs(self.output_dir, exist_ok=True)

        character_id, character_name = self.client.primary_character()
        result.character_name = character_name or ""
        self.events.message(
            "info", f"Primary character: {character_name or character_id}")

        if self.mode == "tpose":
            self._download_tpose(character_id, character_name, result)
            self.events.message("info", result.summary())
            return result

        animations = self._collect_animations()

        state = self.state
        if state is None:
            state = DownloadState.load(self.output_dir, character_id)

        todo, skipped = state.pending(animations, resume=self.resume,
                                      extension=self.client.extension)

        result.total = len(animations)
        result.skipped = skipped

        if skipped:
            self.events.message(
                "info",
                f"Resuming: {skipped} of {len(animations)} animations are "
                f"already on disk.")

        if not todo:
            state.save()
            self.events.total(len(animations))
            self.events.progress(len(animations), len(animations))
            self.events.message("info", "Nothing left to download.")
            return result

        self.events.total(len(todo))
        self.events.message("info", f"Downloading {len(todo)} animations to "
                                    f"{self.output_dir}")

        consecutive_failures = 0
        done = 0

        for anim_id, name in todo:
            if self.stop.is_set():
                result.stopped = True
                break

            self.events.item_started(name)

            try:
                filename = self._attempt_item(character_id, anim_id, name)
            except Stopped:
                result.stopped = True
                break
            except (AuthError, TokenUnavailable) as exc:
                # The token is gone for good: stopping here keeps the resume
                # manifest accurate instead of failing every remaining item.
                state.save()
                result.aborted = True
                result.error = str(exc)
                self.events.message("error", str(exc))
                return result
            except MixamoError as exc:
                consecutive_failures += 1
                result.failed += 1
                result.failures.append((name, str(exc)))
                state.mark_failed(anim_id, name, exc)
                state.save()

                self.events.item_failed(name, str(exc))
                self.events.message("warning", f"{name}: {exc}")

                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    result.aborted = True
                    result.error = (
                        f"{consecutive_failures} animations failed in a row; "
                        f"stopping. Check your Mixamo session and try again "
                        f"-- finished animations will be skipped.")
                    self.events.message("error", result.error)
                    return result

                continue
            except OSError as exc:
                state.save()
                result.aborted = True
                result.error = f"Cannot write to {self.output_dir}: {exc}"
                self.events.message("error", result.error)
                return result

            consecutive_failures = 0
            done += 1
            result.downloaded += 1

            state.mark_done(anim_id, filename)
            state.save()

            self.events.item_done(name)
            self.events.progress(done, len(todo))

        state.save()
        self.events.message(
            "warning" if result.stopped else "info", result.summary())

        return result

    def _attempt_item(self, character_id, anim_id, name):
        """Download one animation, retrying transient failures.

        :param character_id: Primary character ID
        :type character_id: str

        :param anim_id: Animation ID
        :type anim_id: str

        :param name: Animation description
        :type name: str

        :return: File name written to disk
        :rtype: str
        """
        attempts = max(1, self.item_retries + 1)
        last_error = None

        for attempt in range(attempts):
            try:
                return self._download_one(character_id, anim_id, name)
            except RETRYABLE as exc:
                last_error = exc
                if attempt == attempts - 1:
                    break
                self.events.message(
                    "warning", f"{name}: {exc} -- retrying.")

        raise last_error
