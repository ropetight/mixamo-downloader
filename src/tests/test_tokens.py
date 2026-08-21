"""Tests for the access token provider."""

# Stdlib modules
import base64
import json
import threading

# Third-party modules
import pytest

# Local modules
from mixamo.errors import Stopped, TokenUnavailable
from mixamo.tokens import TokenProvider, decode_expiry


def make_jwt(**claims):
    """Build an unsigned JWT carrying the given claims.

    :rtype: str
    """
    def encode(payload):
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


class TestDecodeExpiry:
    """The tool must be able to tell when a token is about to die."""

    def test_reads_the_standard_exp_claim(self):
        assert decode_expiry(make_jwt(exp=1700000000)) == 1700000000.0

    def test_reads_adobe_style_created_at_and_expires_in(self):
        token = make_jwt(created_at="1700000000000", expires_in="86400000")

        assert decode_expiry(token) == 1700086400.0

    @pytest.mark.parametrize("token", [
        None, "", "not-a-jwt", "a.b", "a.!!!.c", make_jwt(sub="no-expiry"),
    ])
    def test_returns_none_when_the_expiry_cannot_be_read(self, token):
        assert decode_expiry(token) is None


class TestTokenProvider:
    """The provider is what keeps long downloads alive."""

    def test_returns_a_token_that_is_not_close_to_expiring(self):
        clock = lambda: 1000.0
        provider = TokenProvider(margin=60, clock=clock)
        provider.set(make_jwt(exp=5000))

        assert provider.get() == provider._token

    def test_refreshes_before_the_token_expires(self):
        # A token with 10 seconds left must not be handed out when the margin
        # is 60: that is exactly the case that killed the old downloader.
        now = [1000.0]
        provider = TokenProvider(margin=60, clock=lambda: now[0])
        provider.set(make_jwt(exp=1010))

        refreshed = make_jwt(exp=9000)
        provider.request_refresh = lambda: provider.set(refreshed)

        assert provider.get() == refreshed

    def test_uses_a_token_with_an_unknown_expiry_until_it_is_rejected(self):
        provider = TokenProvider()
        provider.set("opaque-token")

        assert provider.get() == "opaque-token"

    def test_invalidate_forces_a_refresh(self):
        calls = []
        provider = TokenProvider(request_refresh=lambda: calls.append(1))
        provider.set("first")
        provider.invalidate()

        provider.request_refresh = lambda: (calls.append(1),
                                             provider.set("second"))

        assert provider.get() == "second"
        assert len(calls) == 1

    def test_raises_when_no_fresh_token_ever_arrives(self):
        provider = TokenProvider(
            request_refresh=lambda: None, wait_timeout=0.05)
        provider.invalidate()

        with pytest.raises(TokenUnavailable):
            provider.get()

    def test_raises_when_there_is_no_way_to_refresh(self):
        provider = TokenProvider(request_refresh=None)

        with pytest.raises(TokenUnavailable):
            provider.get()

    def test_stops_waiting_when_the_user_presses_stop(self):
        stop = threading.Event()
        provider = TokenProvider(
            request_refresh=lambda: stop.set(), stop=stop, wait_timeout=5)
        provider.invalidate()

        with pytest.raises(Stopped):
            provider.get()

    def test_a_burst_of_calls_triggers_a_single_refresh(self):
        calls = []
        provider = TokenProvider(request_refresh=lambda: calls.append(1),
                                 wait_timeout=1.0)
        provider.invalidate()

        results = []
        threads = [threading.Thread(target=lambda: results.append(
            provider.get())) for _ in range(4)]

        for thread in threads:
            thread.start()

        # Let every thread reach the wait before the token shows up.
        threading.Event().wait(0.1)
        provider.set("shared-token")

        for thread in threads:
            thread.join(timeout=3)

        assert results == ["shared-token"] * 4
        assert len(calls) == 1

    def test_seconds_left_tracks_the_clock(self):
        now = [1000.0]
        provider = TokenProvider(clock=lambda: now[0])
        provider.set(make_jwt(exp=1100))

        assert provider.seconds_left() == 100
        now[0] = 1090.0
        assert provider.seconds_left() == 10
