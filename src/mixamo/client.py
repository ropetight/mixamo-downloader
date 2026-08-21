"""HTTP layer for the Mixamo API.

Every call made here is bounded: it has a connect/read timeout, a retry
budget with exponential backoff, and a stop check between attempts. That is
what makes the Stop button actually stop things, and what stops the tool
from hanging forever on a half-open socket.
"""

# Stdlib modules
import json
import os
import re
import threading
import time

# Third-party modules
import requests

# Local modules
from .errors import (ApiError, AuthError, ExportFailed, ExportTimeout,
                     RateLimited, Stopped, TransientError)


API = "https://www.mixamo.com/api/v1"

BASE_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate, br",
    "Content-Type": "application/json",
    "X-Api-Key": "mixamo2",
    "X-Requested-With": "XMLHttpRequest",
}

# Status codes worth trying again: throttling, gateway hiccups, timeouts.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# Characters Windows and Linux disagree about; strip them from file names.
ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name, fallback="animation"):
    """Turn an animation description into a file name usable on any OS.

    :param name: Animation description, as returned by Mixamo
    :type name: str

    :param fallback: Name to use when nothing usable is left
    :type fallback: str

    :return: Sanitized file name (no extension)
    :rtype: str
    """
    cleaned = ILLEGAL_CHARS.sub("_", str(name or ""))
    # Collapse the runs of underscores a messy description can produce, and
    # trim the leading/trailing noise a stripped character leaves behind.
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip(" ._")
    return cleaned[:150].strip(" ._") or fallback


class MixamoClient:
    """Talks to the Mixamo API on behalf of the downloader."""

    def __init__(self, tokens, session=None, stop=None, timeout=(10, 60),
                 max_retries=4, backoff=1.5, sleep=None,
                 poll_interval=1.0, export_timeout=300.0, clock=time.monotonic):
        """Initialize the client.

        :param tokens: Provider handing out (and refreshing) bearer tokens
        :type tokens: mixamo.tokens.TokenProvider

        :param session: HTTP session (injected so tests can fake it)
        :type session: requests.Session or None

        :param stop: Event that aborts in-flight work once set
        :type stop: threading.Event or None

        :param timeout: (connect, read) timeout applied to every request
        :type timeout: tuple

        :param max_retries: Attempts per request before giving up
        :type max_retries: int

        :param backoff: Base of the exponential backoff, in seconds
        :type backoff: float

        :param sleep: Interruptible sleep (injected so tests run instantly)
        :type sleep: callable or None

        :param poll_interval: Delay between export monitor polls
        :type poll_interval: float

        :param export_timeout: How long an export may take before we give up
        :type export_timeout: float

        :param clock: Monotonic time source
        :type clock: callable
        """
        self.tokens = tokens
        self.session = session if session is not None else requests.Session()
        self.stop = stop or threading.Event()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.poll_interval = poll_interval
        self.export_timeout = export_timeout
        self.clock = clock
        self._sleep = sleep or self._interruptible_sleep

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _interruptible_sleep(self, seconds):
        """Sleep, but wake up immediately if the user pressed Stop.

        :param seconds: How long to sleep
        :type seconds: float

        :raises Stopped: The stop event was set
        """
        if seconds > 0 and self.stop.wait(seconds):
            raise Stopped("Stopped while waiting to retry.")
        self._check_stop()

    def _check_stop(self):
        """Raise :class:`Stopped` if the user asked us to stop."""
        if self.stop.is_set():
            raise Stopped("Stopped by user.")

    def _headers(self):
        """Build request headers carrying a currently valid token.

        :return: Headers including the Authorization bearer token
        :rtype: dict
        """
        headers = dict(BASE_HEADERS)
        headers["Authorization"] = f"Bearer {self.tokens.get()}"
        return headers

    def _retry_delay(self, attempt, response=None):
        """Work out how long to wait before the next attempt.

        Honours the server's Retry-After header when it sends one.

        :param attempt: Zero-based attempt number that just failed
        :type attempt: int

        :param response: Response that triggered the retry, if any
        :type response: requests.Response or None

        :return: Delay in seconds
        :rtype: float
        """
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except (TypeError, ValueError):
                    pass

        return self.backoff * (2 ** attempt)

    def request(self, method, url, **kwargs):
        """Send a request, retrying transient failures and refreshing tokens.

        :param method: HTTP verb
        :type method: str

        :param url: Absolute URL
        :type url: str

        :return: Successful response
        :rtype: requests.Response

        :raises Stopped: The user pressed Stop
        :raises AuthError: Mixamo rejected the token twice in a row
        :raises RateLimited: Throttled beyond the retry budget
        :raises TransientError: Network or 5xx failure beyond the budget
        :raises ApiError: Any other unexpected status code
        """
        kwargs.setdefault("timeout", self.timeout)
        authenticated = kwargs.pop("authenticated", True)

        last_error = None
        refreshed = False

        for attempt in range(self.max_retries):
            self._check_stop()

            if authenticated:
                kwargs["headers"] = self._headers()

            try:
                response = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_error = TransientError(f"{method} {url} failed: {exc}")
                if attempt == self.max_retries - 1:
                    break
                self._sleep(self._retry_delay(attempt))
                continue

            status = getattr(response, "status_code", 0)

            if 200 <= status < 300:
                return response

            if status in (401, 403) and authenticated:
                # The token went stale mid-run. Drop it, get a new one and
                # replay the request once before treating this as fatal.
                self.tokens.invalidate()
                if refreshed:
                    raise AuthError(
                        "Mixamo rejected the access token. Log into Mixamo "
                        "again in the browser above.")
                refreshed = True
                last_error = AuthError(f"{method} {url} returned {status}.")
                continue

            if status in RETRY_STATUS:
                last_error = (RateLimited(f"{method} {url} returned 429.")
                              if status == 429
                              else TransientError(
                                  f"{method} {url} returned {status}."))
                if attempt == self.max_retries - 1:
                    break
                self._sleep(self._retry_delay(attempt, response))
                continue

            raise ApiError(f"{method} {url} returned {status}.")

        raise last_error or TransientError(f"{method} {url} failed.")

    def get_json(self, url, **kwargs):
        """Send a GET and decode the JSON body.

        :param url: Absolute URL
        :type url: str

        :return: Decoded JSON payload
        :rtype: dict

        :raises ApiError: The body was not valid JSON
        """
        response = self.request("GET", url, **kwargs)
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ApiError(f"GET {url} returned a non-JSON body: {exc}")

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def primary_character(self):
        """Get the ID and name of the user's primary character.

        :return: (character_id, character_name)
        :rtype: tuple

        :raises ApiError: No primary character is set on the account
        """
        data = self.get_json(f"{API}/characters/primary")

        character_id = data.get("primary_character_id")
        character_name = data.get("primary_character_name")

        if not character_id:
            raise ApiError(
                "No primary character found. Upload or select a character "
                "in Mixamo before starting the download.")

        return character_id, character_name

    def search_animations(self, query, limit=96):
        """Get every animation matching a search query.

        :param query: Keyword to search for
        :type query: str

        :param limit: Results per page
        :type limit: int

        :return: Mapping of animation ID to description
        :rtype: dict
        """
        animations = {}
        page = 1
        num_pages = 1

        while page <= num_pages:
            self._check_stop()

            params = {
                "limit": limit,
                "page": page,
                "type": "Motion",
                "query": query,
            }

            data = self.get_json(f"{API}/products", params=params)

            # The original code read the page count once but never advanced
            # the 'page' parameter, so it downloaded page 1 num_pages times.
            num_pages = data.get("pagination", {}).get("num_pages", 1)

            for animation in data.get("results", []):
                animations[animation["id"]] = animation.get(
                    "description") or animation.get("name") or animation["id"]

            page += 1

        return animations

    def animation_payload(self, character_id, anim_id, fps="24", reducekf="0"):
        """Build the export payload for one animation on one character.

        :param character_id: Primary character ID
        :type character_id: str

        :param anim_id: Animation ID
        :type anim_id: str

        :return: (payload JSON string, animation description)
        :rtype: tuple
        """
        details = self.get_json(
            f"{API}/products/{anim_id}",
            params={"similar": 0, "character_id": character_id})

        description = details.get("description") or anim_id
        product_type = details.get("type", "Motion")

        gms_hash = dict(details["details"]["gms_hash"])

        # Mixamo returns each parameter as a list whose last item is the
        # value; the export endpoint wants them as a comma separated string.
        params = gms_hash.get("params") or []
        gms_hash["params"] = ",".join(str(int(param[-1])) for param in params)
        gms_hash["overdrive"] = 0

        trim = gms_hash.get("trim") or [0, 100]
        gms_hash["trim"] = [int(trim[0]), int(trim[1])]

        payload = {
            "character_id": character_id,
            "product_name": description,
            "type": product_type,
            "preferences": {
                "format": "fbx7_2019",
                "skin": False,
                "fps": fps,
                "reducekf": reducekf,
            },
            "gms_hash": [gms_hash],
        }

        return json.dumps(payload), description

    def tpose_payload(self, character_id, character_name):
        """Build the export payload for the character's T-Pose.

        :param character_id: Primary character ID
        :type character_id: str

        :param character_name: Primary character name
        :type character_name: str

        :return: Payload JSON string
        :rtype: str
        """
        payload = {
            "character_id": character_id,
            "product_name": character_name,
            "type": "Character",
            "preferences": {"format": "fbx7_2019", "mesh": "t-pose"},
            "gms_hash": None,
        }

        return json.dumps(payload)

    def export(self, character_id, payload):
        """Kick off an export job and wait for its download link.

        :param character_id: Primary character ID
        :type character_id: str

        :param payload: Export payload JSON string
        :type payload: str

        :return: URL to download the exported FBX
        :rtype: str

        :raises ExportFailed: Mixamo reported the job as failed
        :raises ExportTimeout: The job never completed in time
        """
        self.request("POST", f"{API}/animations/export", data=payload)

        deadline = self.clock() + self.export_timeout
        monitor_url = f"{API}/characters/{character_id}/monitor"

        while True:
            self._check_stop()

            data = self.get_json(monitor_url)
            status = data.get("status")

            if status == "completed":
                link = data.get("job_result")
                if not link:
                    raise ExportFailed("Mixamo completed the export but "
                                       "returned no download link.")
                return link

            if status in ("failed", "error"):
                message = data.get("message") or "Mixamo export job failed."
                raise ExportFailed(message)

            if self.clock() >= deadline:
                # The original code looped here forever on a failed job.
                raise ExportTimeout(
                    f"Export did not finish within "
                    f"{int(self.export_timeout)}s (last status: {status}).")

            self._sleep(self.poll_interval)

    def download(self, url, dest_path, chunk_size=65536):
        """Stream an exported FBX to disk.

        The file is written to a temporary '.part' file and only renamed once
        the download completes, so an interrupted run never leaves a
        truncated FBX that a later resume would mistake for a finished one.

        :param url: Download link returned by the export job
        :type url: str

        :param dest_path: Final path of the FBX on disk
        :type dest_path: str

        :return: Number of bytes written
        :rtype: int

        :raises Stopped: The user pressed Stop mid-download
        """
        response = self.request("GET", url, stream=True, authenticated=False)

        directory = os.path.dirname(dest_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        part_path = f"{dest_path}.part"
        written = 0

        try:
            with open(part_path, "wb") as part_file:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    # Checking between chunks is what lets Stop interrupt a
                    # download instead of waiting for the whole file.
                    self._check_stop()
                    if chunk:
                        part_file.write(chunk)
                        written += len(chunk)

            if written == 0:
                raise TransientError(f"Downloaded an empty file from {url}.")

            os.replace(part_path, dest_path)
        except BaseException:
            # Never leave a half-written '.part' behind.
            try:
                os.remove(part_path)
            except OSError:
                pass
            raise
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        return written
