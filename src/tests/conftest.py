"""Shared test fixtures and HTTP fakes.

Nothing here touches the network, the filesystem outside tmp_path, or Qt,
so the whole suite runs offline in well under a second.
"""

# Stdlib modules
import json
import os
import sys
import threading

# Third-party modules
import pytest
import requests

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Local modules
from mixamo.client import MixamoClient  # noqa: E402
from mixamo.tokens import TokenProvider  # noqa: E402


# A believable binary FBX body: the client checks this magic before it keeps
# a downloaded file, so fixtures have to look like the real thing.
FBX_HEADER = b"Kaydara FBX Binary  \x00\x1a\x00"


def fbx_bytes(payload=b"animation-data"):
    """Build a body the client will accept as a binary FBX.

    :param payload: Bytes appended after the header
    :type payload: bytes

    :rtype: bytes
    """
    return FBX_HEADER + payload


class FakeResponse:
    """Stand-in for a requests.Response."""

    def __init__(self, status_code=200, json_data=None, content=b"",
                 headers=None, chunks=None):
        """Initialize the fake response.

        :param status_code: HTTP status code to report
        :type status_code: int

        :param json_data: Payload returned by :meth:`json`
        :type json_data: object or None

        :param content: Raw body
        :type content: bytes

        :param headers: Response headers
        :type headers: dict or None

        :param chunks: Chunks handed out by :meth:`iter_content`
        :type chunks: list or None
        """
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.headers = headers or {}
        self._chunks = chunks
        self.closed = False

    def json(self):
        """Return the JSON payload, mimicking requests' error on a bad body."""
        if self._json is None:
            raise ValueError("No JSON object could be decoded")
        return self._json

    def iter_content(self, chunk_size=1):
        """Yield the body in chunks.

        :param chunk_size: Requested chunk size
        :type chunk_size: int
        """
        if self._chunks is not None:
            for chunk in self._chunks:
                # A callable chunk lets a test run code mid-download, which is
                # how the Stop-during-download case is exercised.
                yield chunk() if callable(chunk) else chunk
            return

        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        """Record that the response was closed."""
        self.closed = True


class FakeSession:
    """Stand-in for a requests.Session driven by a handler callable."""

    def __init__(self, handler):
        """Initialize the fake session.

        :param handler: Callable receiving (method, url, kwargs). It returns a
            :class:`FakeResponse`, or raises to simulate a network failure.
        :type handler: callable
        """
        self.handler = handler
        self.calls = []

    def request(self, method, url, **kwargs):
        """Record and dispatch a request."""
        self.calls.append((method, url, kwargs))

        result = self.handler(method, url, kwargs)

        if isinstance(result, BaseException):
            raise result

        return result


def queue_handler(responses):
    """Build a handler that hands out queued responses in order.

    :param responses: Responses (or exceptions) to return, oldest first
    :type responses: list

    :rtype: callable
    """
    pending = list(responses)

    def handler(method, url, kwargs):
        if not pending:
            raise AssertionError(f"Unexpected extra request: {method} {url}")
        return pending.pop(0)

    return handler


def route_handler(routes, default=None):
    """Build a handler dispatching on a substring of the URL.

    :param routes: Mapping of URL fragment to a response, list of responses,
        or callable taking (method, url, kwargs)
    :type routes: dict

    :param default: Response used when no route matches
    :type default: FakeResponse or None

    :rtype: callable
    """
    queues = {key: list(value) if isinstance(value, list) else value
              for key, value in routes.items()}

    def handler(method, url, kwargs):
        for fragment, value in queues.items():
            if fragment in url:
                if isinstance(value, list):
                    if not value:
                        raise AssertionError(
                            f"No responses left for {fragment}")
                    return value.pop(0)
                if callable(value) and not isinstance(value, FakeResponse):
                    return value(method, url, kwargs)
                return value

        if default is not None:
            return default

        raise AssertionError(f"No route matches {method} {url}")

    return handler


class InstantSleep:
    """Sleep replacement that records delays instead of waiting."""

    def __init__(self, stop=None):
        """Initialize the fake sleeper.

        :param stop: Event checked after each call, mirroring the real sleep
        :type stop: threading.Event or None
        """
        self.delays = []
        self.stop = stop

    def __call__(self, seconds):
        """Record a delay and return immediately."""
        self.delays.append(seconds)

        if self.stop is not None and self.stop.is_set():
            from mixamo.errors import Stopped
            raise Stopped("Stopped while waiting to retry.")


@pytest.fixture
def stop_event():
    """A fresh stop event."""
    return threading.Event()


@pytest.fixture
def tokens(stop_event):
    """A token provider preloaded with a token that never expires."""
    provider = TokenProvider(request_refresh=None, stop=stop_event)
    provider.set("test-token")
    return provider


@pytest.fixture
def make_client(tokens, stop_event):
    """Factory building a client wired to a fake session.

    :return: Callable taking a handler and returning (client, session)
    :rtype: callable
    """
    def factory(handler, provider=None, stop=None, **kwargs):
        session = FakeSession(handler)
        event = stop if stop is not None else stop_event
        sleeper = InstantSleep(stop=event)

        client = MixamoClient(
            provider or tokens,
            session=session,
            stop=event,
            sleep=sleeper,
            backoff=0.01,
            poll_interval=0.0,
            **kwargs)

        client.sleeper = sleeper
        return client, session

    return factory


@pytest.fixture
def anims_file(tmp_path):
    """A small stand-in for the shipped mixamo_anims.json.

    :rtype: str
    """
    path = tmp_path / "anims.json"
    path.write_text(json.dumps({
        "id-1": "Walking",
        "id-2": "Running",
        "id-3": "Zombie Idle",
    }))
    return str(path)


def network_error(message="connection reset"):
    """Build a requests exception standing in for a dropped connection.

    :rtype: requests.RequestException
    """
    return requests.RequestException(message)


@pytest.fixture(scope="session")
def qt_app():
    """A QApplication for the whole session, or a skip if Qt cannot start.

    Imported lazily: the tests that never touch the UI should not pay for
    loading Qt at collection time.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        from PySide6 import QtWidgets
    except ImportError:  # pragma: no cover - PySide6 is a hard dependency
        pytest.skip("PySide6 is not available")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    yield app


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    """A main window that neither loads a page nor touches real settings."""
    import webpage

    # Loading mixamo.com would make the suite need a network.
    monkeypatch.setattr(webpage.CustomWebPage, "setUrl",
                        lambda self, url: None)

    from PySide6 import QtCore

    # Keep the developer's own saved options out of the tests, and the
    # tests out of the developer's options.
    monkeypatch.setattr(
        QtCore.QSettings, "fileName", lambda self: str(tmp_path / "s.ini"))

    from ui import MixamoDownloaderUI

    made = MixamoDownloaderUI()
    made.settings = QtCore.QSettings(
        str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat)

    yield made

    if made.browser is not None or made.page is not None:
        made.shutdown()
