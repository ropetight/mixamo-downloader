"""Tests for the Mixamo HTTP client."""

# Stdlib modules
import os
import threading

# Third-party modules
import pytest

# Local modules
from conftest import (FakeResponse, network_error, queue_handler,
                      route_handler)
from mixamo.client import API, MixamoClient, safe_filename
from mixamo.errors import (ApiError, AuthError, ExportFailed, ExportTimeout,
                           RateLimited, Stopped, TransientError)
from mixamo.tokens import TokenProvider


class TestSafeFilename:
    """Animation descriptions become file names, so they must be sanitized."""

    def test_replaces_characters_that_are_illegal_in_file_names(self):
        assert safe_filename('Walk/Run: "fast"') == "Walk_Run_ _fast"

    def test_trims_leading_and_trailing_noise(self):
        assert safe_filename("  Walking...  ") == "Walking"

    def test_collapses_repeated_underscores(self):
        assert safe_filename("a///b") == "a_b"

    def test_falls_back_when_nothing_usable_is_left(self):
        assert safe_filename("...") == "animation"
        assert safe_filename("") == "animation"

    def test_truncates_absurdly_long_names(self):
        assert len(safe_filename("x" * 400)) == 150


class TestRequestRetries:
    """Every request is bounded and retried; nothing hangs forever."""

    def test_retries_a_server_error_and_then_succeeds(self, make_client):
        client, session = make_client(queue_handler([
            FakeResponse(503),
            FakeResponse(200, json_data={"ok": True}),
        ]))

        assert client.get_json(f"{API}/anything") == {"ok": True}
        assert len(session.calls) == 2

    def test_retries_a_dropped_connection(self, make_client):
        client, session = make_client(queue_handler([
            network_error(),
            network_error(),
            FakeResponse(200, json_data={"ok": True}),
        ]))

        assert client.get_json(f"{API}/anything") == {"ok": True}
        assert len(session.calls) == 3

    def test_gives_up_after_the_retry_budget(self, make_client):
        client, _ = make_client(
            queue_handler([FakeResponse(500)] * 4), max_retries=4)

        with pytest.raises(TransientError):
            client.get_json(f"{API}/anything")

    def test_reports_throttling_separately(self, make_client):
        client, _ = make_client(
            queue_handler([FakeResponse(429)] * 4), max_retries=4)

        with pytest.raises(RateLimited):
            client.get_json(f"{API}/anything")

    def test_honours_the_retry_after_header(self, make_client):
        client, _ = make_client(queue_handler([
            FakeResponse(429, headers={"Retry-After": "7"}),
            FakeResponse(200, json_data={}),
        ]))

        client.get_json(f"{API}/anything")

        assert client.sleeper.delays == [7.0]

    def test_backs_off_exponentially(self, make_client):
        client, _ = make_client(queue_handler([
            FakeResponse(500), FakeResponse(500),
            FakeResponse(200, json_data={}),
        ]))

        client.get_json(f"{API}/anything")

        assert client.sleeper.delays == [0.01, 0.02]

    def test_does_not_retry_a_client_error(self, make_client):
        client, session = make_client(queue_handler([FakeResponse(404)]))

        with pytest.raises(ApiError):
            client.get_json(f"{API}/anything")

        assert len(session.calls) == 1

    def test_rejects_a_non_json_body(self, make_client):
        client, _ = make_client(queue_handler([FakeResponse(200)]))

        with pytest.raises(ApiError):
            client.get_json(f"{API}/anything")


class TestStopping:
    """The Stop button has to interrupt work in progress, not queue behind it."""

    def test_stops_before_sending_a_request(self, make_client, stop_event):
        stop_event.set()
        client, session = make_client(queue_handler([]))

        with pytest.raises(Stopped):
            client.get_json(f"{API}/anything")

        assert session.calls == []

    def test_stops_while_waiting_to_retry(self, make_client, stop_event):
        def handler(method, url, kwargs):
            stop_event.set()
            return FakeResponse(503)

        client, session = make_client(handler)

        with pytest.raises(Stopped):
            client.get_json(f"{API}/anything")

        assert len(session.calls) == 1


class TestAuthentication:
    """A token that expires mid-run must be refreshed, not fatal."""

    def _provider(self, tokens_sequence, stop):
        """Build a provider handing out the given tokens in order."""
        pending = list(tokens_sequence)
        provider = TokenProvider(stop=stop, wait_timeout=1.0)
        provider.set(pending.pop(0))
        provider.request_refresh = lambda: provider.set(pending.pop(0))
        return provider

    def test_refreshes_the_token_and_replays_the_request(self, make_client,
                                                         stop_event):
        provider = self._provider(["stale", "fresh"], stop_event)
        client, session = make_client(queue_handler([
            FakeResponse(401),
            FakeResponse(200, json_data={"ok": True}),
        ]), provider=provider)

        assert client.get_json(f"{API}/anything") == {"ok": True}

        first, second = session.calls
        assert first[2]["headers"]["Authorization"] == "Bearer stale"
        assert second[2]["headers"]["Authorization"] == "Bearer fresh"

    def test_gives_up_when_the_fresh_token_is_rejected_too(self, make_client,
                                                           stop_event):
        provider = self._provider(["stale", "also-stale"], stop_event)
        client, _ = make_client(queue_handler([
            FakeResponse(401), FakeResponse(403),
        ]), provider=provider)

        with pytest.raises(AuthError):
            client.get_json(f"{API}/anything")

    def test_a_rejected_download_link_does_not_burn_the_token(
            self, make_client, tokens, tmp_path):
        # Pre-signed links expire on their own; that says nothing about the
        # Mixamo session, so the token must survive a 403 from the CDN.
        client, _ = make_client(queue_handler([FakeResponse(403)]))

        with pytest.raises(ApiError):
            client.download("https://cdn.example/f.fbx",
                            str(tmp_path / "a.fbx"))

        assert tokens.get() == "test-token"

    def test_sends_no_token_when_downloading_the_exported_file(
            self, make_client):
        # The export link is a pre-signed URL; sending Adobe's bearer token to
        # it is unnecessary and can break the download.
        client, session = make_client(
            queue_handler([FakeResponse(200, content=b"fbx")]))

        client.download("https://cdn.example/file.fbx",
                        os.path.join(os.getcwd(), "unused"))

        assert "headers" not in session.calls[0][2]
        os.remove(os.path.join(os.getcwd(), "unused"))


class TestPrimaryCharacter:
    """The run cannot start without a character selected in Mixamo."""

    def test_returns_the_id_and_name(self, make_client):
        client, _ = make_client(queue_handler([FakeResponse(200, json_data={
            "primary_character_id": "char-1",
            "primary_character_name": "Victoria",
        })]))

        assert client.primary_character() == ("char-1", "Victoria")

    def test_explains_what_to_do_when_none_is_set(self, make_client):
        client, _ = make_client(
            queue_handler([FakeResponse(200, json_data={})]))

        with pytest.raises(ApiError, match="primary character"):
            client.primary_character()


class TestSearch:
    """Searching must walk every page exactly once."""

    def test_walks_every_page(self, make_client):
        pages = {
            1: {"pagination": {"num_pages": 3},
                "results": [{"id": "a", "description": "Walk"}]},
            2: {"pagination": {"num_pages": 3},
                "results": [{"id": "b", "description": "Walk Back"}]},
            3: {"pagination": {"num_pages": 3},
                "results": [{"id": "c", "description": "Walk Left"}]},
        }
        seen = []

        def handler(method, url, kwargs):
            page = kwargs["params"]["page"]
            seen.append(page)
            return FakeResponse(200, json_data=pages[page])

        client, _ = make_client(handler)

        assert client.search_animations("walk") == {
            "a": "Walk", "b": "Walk Back", "c": "Walk Left"}
        # The old implementation requested page 1 three times.
        assert seen == [1, 2, 3]

    def test_handles_an_empty_result(self, make_client):
        client, _ = make_client(queue_handler([FakeResponse(200, json_data={
            "pagination": {"num_pages": 1}, "results": []})]))

        assert client.search_animations("nothing") == {}


class TestPayloads:
    """Export payloads must match what Mixamo expects."""

    def test_builds_an_animation_payload(self, make_client):
        client, _ = make_client(queue_handler([FakeResponse(200, json_data={
            "description": "Zombie Idle",
            "type": "Motion",
            "details": {"gms_hash": {
                "params": [["Overdrive", 0, 1], ["Emotion", 0, 0]],
                "trim": ["0", "100"],
                "model-id": 7,
            }},
        })]))

        payload, description = client.animation_payload("char-1", "anim-1")

        import json as _json
        data = _json.loads(payload)

        assert description == "Zombie Idle"
        assert data["character_id"] == "char-1"
        assert data["product_name"] == "Zombie Idle"
        assert data["gms_hash"][0]["params"] == "1,0"
        assert data["gms_hash"][0]["trim"] == [0, 100]
        assert data["gms_hash"][0]["overdrive"] == 0
        assert data["preferences"]["format"] == "fbx7_2019"

    def test_builds_a_tpose_payload_with_a_skin(self, make_client):
        client, _ = make_client(queue_handler([]))

        import json as _json
        data = _json.loads(client.tpose_payload("char-1", "Victoria"))

        assert data["type"] == "Character"
        assert data["preferences"]["mesh"] == "t-pose"
        assert data["gms_hash"] is None


class TestExport:
    """Polling the export monitor must always terminate."""

    def test_polls_until_the_job_completes(self, make_client):
        client, _ = make_client(route_handler({
            "/animations/export": FakeResponse(200, json_data={}),
            "/monitor": [
                FakeResponse(200, json_data={"status": "processing"}),
                FakeResponse(200, json_data={"status": "processing"}),
                FakeResponse(200, json_data={"status": "completed",
                                             "job_result": "https://cdn/f.fbx"}),
            ],
        }))

        assert client.export("char-1", "{}") == "https://cdn/f.fbx"

    def test_raises_when_mixamo_reports_a_failed_job(self, make_client):
        # The old code looped forever on this exact response.
        client, _ = make_client(route_handler({
            "/animations/export": FakeResponse(200, json_data={}),
            "/monitor": FakeResponse(200, json_data={
                "status": "failed", "message": "rig mismatch"}),
        }))

        with pytest.raises(ExportFailed, match="rig mismatch"):
            client.export("char-1", "{}")

    def test_gives_up_on_a_job_that_never_finishes(self, make_client):
        ticks = iter(range(0, 10000, 60))

        client, _ = make_client(route_handler({
            "/animations/export": FakeResponse(200, json_data={}),
            "/monitor": FakeResponse(200, json_data={"status": "processing"}),
        }), export_timeout=120, clock=lambda: next(ticks))

        with pytest.raises(ExportTimeout):
            client.export("char-1", "{}")

    def test_raises_when_a_completed_job_has_no_link(self, make_client):
        client, _ = make_client(route_handler({
            "/animations/export": FakeResponse(200, json_data={}),
            "/monitor": FakeResponse(200, json_data={"status": "completed"}),
        }))

        with pytest.raises(ExportFailed):
            client.export("char-1", "{}")

    def test_stop_interrupts_the_poll_loop(self, make_client, stop_event):
        def monitor(method, url, kwargs):
            stop_event.set()
            return FakeResponse(200, json_data={"status": "processing"})

        client, _ = make_client(route_handler({
            "/animations/export": FakeResponse(200, json_data={}),
            "/monitor": monitor,
        }))

        with pytest.raises(Stopped):
            client.export("char-1", "{}")


class TestDownload:
    """Downloads must be interruptible and never leave a truncated FBX."""

    def test_writes_the_file(self, make_client, tmp_path):
        client, _ = make_client(
            queue_handler([FakeResponse(200, content=b"FBX-DATA")]))

        dest = tmp_path / "Walking.fbx"
        written = client.download("https://cdn/f.fbx", str(dest))

        assert written == 8
        assert dest.read_bytes() == b"FBX-DATA"

    def test_creates_the_output_folder(self, make_client, tmp_path):
        client, _ = make_client(
            queue_handler([FakeResponse(200, content=b"FBX")]))

        dest = tmp_path / "nested" / "deeper" / "Walking.fbx"
        client.download("https://cdn/f.fbx", str(dest))

        assert dest.exists()

    def test_leaves_no_partial_file_when_stopped(self, make_client,
                                                 stop_event, tmp_path):
        def stop_now():
            stop_event.set()
            return b"second"

        client, _ = make_client(queue_handler([
            FakeResponse(200, chunks=[b"first", stop_now, b"third"])]))

        dest = tmp_path / "Walking.fbx"

        with pytest.raises(Stopped):
            client.download("https://cdn/f.fbx", str(dest))

        assert not dest.exists()
        assert list(tmp_path.iterdir()) == []

    def test_rejects_an_empty_download(self, make_client, tmp_path):
        client, _ = make_client(
            queue_handler([FakeResponse(200, content=b"")]))

        dest = tmp_path / "Walking.fbx"

        with pytest.raises(TransientError):
            client.download("https://cdn/f.fbx", str(dest))

        # An empty file on disk would be adopted by resume as "already done".
        assert not dest.exists()

    def test_closes_the_response(self, make_client, tmp_path):
        response = FakeResponse(200, content=b"FBX")
        client, _ = make_client(queue_handler([response]))

        client.download("https://cdn/f.fbx", str(tmp_path / "a.fbx"))

        assert response.closed
