"""Access token handling.

Mixamo stores a short-lived bearer token in the browser's localStorage.
The old implementation read it once, at startup, and used it forever.
Any download longer than the token lifetime died silently half-way through.

`TokenProvider` fixes that by owning the token instead: it knows when the
token is about to expire, it asks the UI for a fresh one, and it blocks the
worker thread only for as long as the refresh takes.
"""

# Stdlib modules
import base64
import json
import threading
import time

# Local modules
from .errors import Stopped, TokenUnavailable


def decode_expiry(token):
    """Read the 'exp' claim out of a JWT without validating the signature.

    Mixamo hands out Adobe IMS tokens, which are JWTs. Knowing when they
    expire lets us refresh *before* a request fails instead of after.

    :param token: Bearer token
    :type token: str

    :return: Expiry as a UNIX timestamp, or None if it cannot be read
    :rtype: float or None
    """
    if not token:
        return None

    parts = token.split(".")
    if len(parts) != 3:
        return None

    payload = parts[1]
    # Base64url payloads are stored without their trailing padding.
    payload += "=" * (-len(payload) % 4)

    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except Exception:
        return None

    # Adobe IMS uses milliseconds for 'created_at'/'expires_in', but the
    # standard 'exp' claim is in seconds. Accept either shape.
    exp = claims.get("exp")
    if exp is not None:
        try:
            return float(exp)
        except (TypeError, ValueError):
            return None

    created_at = claims.get("created_at")
    expires_in = claims.get("expires_in")
    if created_at is not None and expires_in is not None:
        try:
            return (float(created_at) + float(expires_in)) / 1000.0
        except (TypeError, ValueError):
            return None

    return None


class TokenProvider:
    """Thread-safe holder for the Mixamo bearer token.

    The worker thread calls :meth:`get`. The UI thread calls :meth:`set`
    whenever it has scraped a token out of the embedded browser.
    """

    def __init__(self, request_refresh=None, stop=None, margin=120.0,
                 wait_timeout=45.0, clock=time.time):
        """Initialize the token provider.

        :param request_refresh: Callable asking the UI for a fresh token.
            It must return immediately; the new token is delivered later
            through :meth:`set`.
        :type request_refresh: callable or None

        :param stop: Event that, once set, aborts any wait
        :type stop: threading.Event or None

        :param margin: Refresh this many seconds before the token expires
        :type margin: float

        :param wait_timeout: How long to wait for a refresh before giving up
        :type wait_timeout: float

        :param clock: Time source (injected so tests can fake expiry)
        :type clock: callable
        """
        self.request_refresh = request_refresh
        self._stop = stop or threading.Event()
        self._margin = margin
        self.wait_timeout = wait_timeout
        self._clock = clock

        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._token = None
        self._expiry = None
        self._refresh_pending = False

    def set(self, token):
        """Store a token. Safe to call from any thread.

        :param token: Bearer token scraped from the browser
        :type token: str
        """
        with self._lock:
            self._token = token or None
            self._expiry = decode_expiry(token)
            self._refresh_pending = False

            if self._token:
                self._ready.set()
            else:
                self._ready.clear()

    def invalidate(self):
        """Drop the current token, forcing a refresh on the next :meth:`get`."""
        with self._lock:
            self._token = None
            self._expiry = None
            self._ready.clear()

    @property
    def expiry(self):
        """Expiry timestamp of the current token, or None if unknown."""
        with self._lock:
            return self._expiry

    def seconds_left(self):
        """Seconds until the token expires, or None if the expiry is unknown."""
        with self._lock:
            if self._expiry is None:
                return None
            return self._expiry - self._clock()

    def _is_fresh(self):
        """Whether the held token exists and is not about to expire."""
        if not self._token:
            return False
        if self._expiry is None:
            # Unknown expiry: trust it until a request comes back 401.
            return True
        return self._expiry - self._clock() > self._margin

    def get(self):
        """Return a usable token, refreshing it first if needed.

        Blocks the calling thread while a refresh is in flight.

        :return: Bearer token
        :rtype: str

        :raises Stopped: The user pressed the Stop button while waiting
        :raises TokenUnavailable: No fresh token arrived in time
        """
        with self._lock:
            if self._is_fresh():
                return self._token

            # The held token is stale: stop handing it out and ask for a new
            # one. Guard with a flag so a burst of calls triggers one refresh.
            self._ready.clear()
            already_pending = self._refresh_pending
            self._refresh_pending = True

        if self.request_refresh is None:
            with self._lock:
                self._refresh_pending = False
                if self._token:
                    # No way to refresh, but we do have something. Use it.
                    return self._token
            raise TokenUnavailable("No access token and no way to refresh it.")

        if not already_pending:
            self.request_refresh()

        deadline = self._clock() + self.wait_timeout

        # Wait in slices so the Stop button stays responsive.
        while True:
            if self._stop.is_set():
                raise Stopped("Stopped while waiting for an access token.")

            if self._ready.wait(0.25):
                with self._lock:
                    if self._token:
                        return self._token

            if self._clock() >= deadline:
                with self._lock:
                    self._refresh_pending = False
                raise TokenUnavailable(
                    "Timed out waiting for a fresh Mixamo access token. "
                    "Make sure you are still logged into Mixamo.")
