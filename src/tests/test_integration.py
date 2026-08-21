"""End-to-end tests wiring the real client, state and job onto a fake network.

The unit tests fake one layer at a time; these make sure the layers actually
fit together, including the token refresh and the resume manifest.
"""

# Stdlib modules
import os
import threading

# Third-party modules
import pytest

# Local modules
from conftest import FakeResponse, FakeSession, InstantSleep
from mixamo.client import MixamoClient
from mixamo.job import DownloadJob, JobEvents
from mixamo.state import DownloadState
from mixamo.tokens import TokenProvider


ANIMS = {"anim-1": "Walking", "anim-2": "Zombie Idle"}


class MixamoStub:
    """Minimal in-memory stand-in for the Mixamo API."""

    def __init__(self, fail_export_for=(), reject_tokens=()):
        """Initialize the stub.

        :param fail_export_for: Animation IDs whose export job fails
        :type fail_export_for: tuple

        :param reject_tokens: Tokens answered with 401
        :type reject_tokens: tuple
        """
        self.fail_export_for = set(fail_export_for)
        self.reject_tokens = set(reject_tokens)
        self.exported = []
        self._last_failed = False

    def __call__(self, method, url, kwargs):
        """Answer one request."""
        headers = kwargs.get("headers") or {}
        token = headers.get("Authorization", "").replace("Bearer ", "")

        if headers and token in self.reject_tokens:
            return FakeResponse(401)

        if "/characters/primary" in url:
            return FakeResponse(200, json_data={
                "primary_character_id": "char-1",
                "primary_character_name": "Victoria"})

        if "/products/" in url:
            anim_id = url.split("/products/")[1].split("?")[0]
            return FakeResponse(200, json_data={
                "description": ANIMS[anim_id],
                "type": "Motion",
                "details": {"gms_hash": {
                    "params": [["Overdrive", 0, 1]],
                    "trim": [0, 100]}}})

        if "/animations/export" in url:
            import json
            payload = json.loads(kwargs["data"])
            name = payload["product_name"]
            self.exported.append(name)
            self._last_failed = any(
                ANIMS[anim_id] == name for anim_id in self.fail_export_for)
            return FakeResponse(200, json_data={})

        if "/monitor" in url:
            if self._last_failed:
                return FakeResponse(200, json_data={
                    "status": "failed", "message": "export failed"})
            return FakeResponse(200, json_data={
                "status": "completed",
                "job_result": "https://cdn.example/file.fbx"})

        if "cdn.example" in url:
            return FakeResponse(200, content=b"FBX-CONTENT")

        raise AssertionError(f"Unexpected request: {method} {url}")


def build(tmp_path, stub, token="token-1", stop=None, provider=None):
    """Wire a real client and job onto the stub.

    :rtype: tuple
    """
    stop = stop if stop is not None else threading.Event()

    tokens = provider or TokenProvider(stop=stop)
    if provider is None:
        tokens.set(token)

    client = MixamoClient(
        tokens,
        session=FakeSession(stub),
        stop=stop,
        sleep=InstantSleep(stop=stop),
        backoff=0.0,
        poll_interval=0.0)

    anims_file = tmp_path / "anims.json"
    import json
    anims_file.write_text(json.dumps(ANIMS))

    job = DownloadJob(client, str(tmp_path / "out"), "all", stop=stop,
                      anims_file=str(anims_file), item_retries=0)

    return job, client, stop


class TestEndToEnd:
    """A full run against a stubbed Mixamo."""

    def test_downloads_and_records_everything(self, tmp_path):
        job, _, _ = build(tmp_path, MixamoStub())

        result = job.run()

        assert result.ok
        assert result.downloaded == 2
        assert (tmp_path / "out" / "Walking.fbx").read_bytes() == b"FBX-CONTENT"
        assert (tmp_path / "out" / "Zombie Idle.fbx").exists()

        state = DownloadState.load(str(tmp_path / "out"), "char-1")
        assert sorted(state.completed) == ["anim-1", "anim-2"]

    def test_a_failed_export_does_not_stop_the_rest(self, tmp_path):
        stub = MixamoStub(fail_export_for=["anim-1"])
        job, _, _ = build(tmp_path, stub)

        result = job.run()

        assert result.downloaded == 1
        assert result.failed == 1
        assert (tmp_path / "out" / "Zombie Idle.fbx").exists()
        assert not (tmp_path / "out" / "Walking.fbx").exists()

    def test_the_second_run_only_picks_up_what_failed(self, tmp_path):
        first_stub = MixamoStub(fail_export_for=["anim-1"])
        job, _, _ = build(tmp_path, first_stub)
        job.run()

        second_stub = MixamoStub()
        job, _, _ = build(tmp_path, second_stub)
        result = job.run()

        assert second_stub.exported == ["Walking"]
        assert result.downloaded == 1
        assert result.skipped == 1

    def test_an_expired_token_is_refreshed_mid_run(self, tmp_path):
        stub = MixamoStub(reject_tokens=["stale-token"])

        refreshes = []
        provider = TokenProvider(wait_timeout=1.0)
        provider.set("stale-token")

        def refresh():
            refreshes.append(True)
            provider.set("good-token")

        provider.request_refresh = refresh

        job, _, _ = build(tmp_path, stub, provider=provider)
        result = job.run()

        assert refreshes == [True]
        assert result.ok
        assert result.downloaded == 2

    def test_stop_leaves_a_resumable_folder(self, tmp_path):
        stop = threading.Event()
        stub = MixamoStub()

        # Stop as soon as the first FBX comes back off the wire.
        original = stub.__call__

        def stopping_stub(method, url, kwargs):
            response = original(method, url, kwargs)
            if "cdn.example" in url and stub.exported:
                stop.set()
            return response

        job, _, _ = build(tmp_path, stopping_stub, stop=stop)
        result = job.run()

        assert result.stopped
        # Nothing half-written was left behind for resume to trust.
        assert not any(name.endswith(".part")
                       for name in os.listdir(tmp_path / "out"))
