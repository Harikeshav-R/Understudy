"""Unit tests for tunnel manager and provider discovery."""

import asyncio
from typing import Any

import pytest

from understudy.signals.tunnel import (
    TunnelSession,
    extract_tunnel_url,
    find_tunnel_provider,
)


def test_find_tunnel_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Fake flag and fake requested
    assert find_tunnel_provider(fake=True) == "fake"
    assert find_tunnel_provider(requested="fake") == "fake"

    # 2. Specific providers present
    monkeypatch.setattr("shutil.which", lambda _p: f"/usr/bin/{_p}")
    assert find_tunnel_provider(requested="cloudflared") == "cloudflared"
    assert find_tunnel_provider(requested="ngrok") == "ngrok"

    # 3. Specific provider missing
    monkeypatch.setattr("shutil.which", lambda _p: None)
    with pytest.raises(RuntimeError, match="Requested tunnel provider 'cloudflared' not found"):
        find_tunnel_provider(requested="cloudflared")

    with pytest.raises(RuntimeError, match="Requested tunnel provider 'ngrok' not found"):
        find_tunnel_provider(requested="ngrok")

    # 4. Auto discovery
    monkeypatch.setattr(
        "shutil.which", lambda _p: "/usr/bin/cloudflared" if _p == "cloudflared" else None
    )
    assert find_tunnel_provider(requested="auto") == "cloudflared"

    monkeypatch.setattr("shutil.which", lambda _p: "/usr/bin/ngrok" if _p == "ngrok" else None)
    assert find_tunnel_provider(requested="auto") == "ngrok"

    monkeypatch.setattr("shutil.which", lambda _p: None)
    with pytest.raises(RuntimeError, match="Neither 'cloudflared' nor 'ngrok' found in PATH"):
        find_tunnel_provider(requested="auto")

    # 5. Unknown provider
    with pytest.raises(RuntimeError, match="Unknown tunnel provider 'unsupported'"):
        find_tunnel_provider(requested="unsupported")


def test_extract_tunnel_url() -> None:
    # cloudflared
    cf_line = "INF +---------------------------------------------------------+"
    assert extract_tunnel_url(cf_line, "cloudflared") is None

    cf_valid = "2026-09-13T09:00:00Z INF | https://happy-cat-123.trycloudflare.com |"
    assert extract_tunnel_url(cf_valid, "cloudflared") == "https://happy-cat-123.trycloudflare.com"

    # ngrok
    ng_line = 't=2026-09-13T09:00:00+0000 lvl=info msg="client session established"'
    assert extract_tunnel_url(ng_line, "ngrok") is None

    ng_valid1 = (
        't=2026-09-13T09:00:00+0000 lvl=info msg="started tunnel" url=https://abc.ngrok-free.app'
    )
    assert extract_tunnel_url(ng_valid1, "ngrok") == "https://abc.ngrok-free.app"

    ng_valid2 = "url=https://custom-domain.ngrok.io"
    assert extract_tunnel_url(ng_valid2, "ngrok") == "https://custom-domain.ngrok.io"

    # other
    assert extract_tunnel_url("https://example.com", "other") is None


@pytest.mark.asyncio
async def test_tunnel_session_fake_lifecycle() -> None:
    session = TunnelSession(port=19208, fake=True, secret="test-secret")
    assert session.provider == "fake"

    async with session:
        assert session.public_url == "https://fake-tunnel-19208.understudy.dev"
        assert session.webhook_url == "https://fake-tunnel-19208.understudy.dev/webhook"

    assert session.proc is None


@pytest.mark.asyncio
async def test_tunnel_session_run_until_cancelled() -> None:
    session = TunnelSession(port=19209, fake=True, secret="test-secret")
    await session.start()

    task = asyncio.create_task(session.run_until_cancelled())
    await asyncio.sleep(0.05)
    task.cancel()
    await task


class DummyStream:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = list(lines)
        self._eof = False

    def at_eof(self) -> bool:
        return self._eof

    async def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        self._eof = True
        return b""


class DummyProcess:
    def __init__(
        self, stdout: DummyStream | None = None, stderr: DummyStream | None = None
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_tunnel_session_cloudflared_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _p: f"/usr/bin/{_p}")
    lines = [
        b"2026-09-13 Starting tunnel...\n",
        b"Your quick Tunnel has been created: https://test-cloud.trycloudflare.com\n",
    ]
    dummy_proc = DummyProcess(stderr=DummyStream(lines))

    async def _mock_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> DummyProcess:
        return dummy_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _mock_create_subprocess_exec)

    session = TunnelSession(port=19210, provider="cloudflared", fake=False, secret="test-secret")
    assert session.provider == "cloudflared"

    async with session:
        assert session.public_url == "https://test-cloud.trycloudflare.com"
        assert session.webhook_url == "https://test-cloud.trycloudflare.com/webhook"

    assert dummy_proc.terminated is True


@pytest.mark.asyncio
async def test_tunnel_session_ngrok_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _p: f"/usr/bin/{_p}")
    lines = [
        b"Connecting...\n",
        b'msg="started tunnel" url=https://test-ngrok.ngrok-free.app\n',
    ]
    dummy_proc = DummyProcess(stdout=DummyStream(lines))

    async def _mock_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> DummyProcess:
        return dummy_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _mock_create_subprocess_exec)

    session = TunnelSession(port=19211, provider="ngrok", fake=False, secret="test-secret")
    assert session.provider == "ngrok"

    async with session:
        assert session.public_url == "https://test-ngrok.ngrok-free.app"

    assert dummy_proc.terminated is True


@pytest.mark.asyncio
async def test_tunnel_session_failed_url_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _p: f"/usr/bin/{_p}")
    lines = [b"No url found here\n", b""]
    dummy_proc = DummyProcess(stderr=DummyStream(lines))

    async def _mock_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> DummyProcess:
        return dummy_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _mock_create_subprocess_exec)

    session = TunnelSession(port=19212, provider="cloudflared", fake=False, secret="test-secret")
    with pytest.raises(RuntimeError, match="Failed to discover public URL"):
        await session.start(timeout_seconds=2.0)


@pytest.mark.asyncio
async def test_tunnel_session_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _p: f"/usr/bin/{_p}")

    class StallingStream(DummyStream):
        async def readline(self) -> bytes:
            await asyncio.sleep(5.0)
            return b""

    dummy_proc = DummyProcess(stderr=StallingStream([]))

    async def _mock_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> DummyProcess:
        return dummy_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _mock_create_subprocess_exec)

    session = TunnelSession(port=19213, provider="cloudflared", fake=False, secret="test-secret")
    with pytest.raises(RuntimeError, match="Timed out waiting for cloudflared tunnel URL"):
        await session.start(timeout_seconds=0.05)


@pytest.mark.asyncio
async def test_tunnel_session_stop_kill_and_process_lookup_error() -> None:
    session = TunnelSession(port=19214, fake=True, secret="test-secret")

    # 1. Stalling wait triggers kill
    class TimeoutProcess(DummyProcess):
        async def wait(self) -> int:
            if not self.killed:
                raise TimeoutError()
            return 0

    proc1 = TimeoutProcess()
    session.proc = proc1  # type: ignore[assignment]
    await session.stop()
    assert proc1.killed is True

    # 2. ProcessLookupError handled cleanly
    class DeadProcess(DummyProcess):
        def terminate(self) -> None:
            raise ProcessLookupError()

    proc2 = DeadProcess()
    session.proc = proc2  # type: ignore[assignment]
    await session.stop()


@pytest.mark.asyncio
async def test_tunnel_session_missing_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _p: f"/usr/bin/{_p}")
    dummy_proc = DummyProcess(stderr=None)

    async def _mock_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> DummyProcess:
        return dummy_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _mock_create_subprocess_exec)

    session = TunnelSession(port=19215, provider="cloudflared", fake=False, secret="test-secret")
    with pytest.raises(RuntimeError, match="Failed to attach to cloudflared process stream"):
        await session.start()
