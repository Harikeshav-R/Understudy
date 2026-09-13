"""Tunnel manager for exposing PagerDuty webhook receiver via public URL."""

import asyncio
import re
import shutil
from typing import Any

from understudy.common.clock import Clock
from understudy.common.logging import get_logger
from understudy.signals.pagerduty import PagerDutyAlertSource, WebhookReceiverServer

CLOUDFLARED_URL_REGEX = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
NGROK_URL_REGEX = re.compile(r"https://[a-zA-Z0-9-]+\.(?:ngrok-free\.app|ngrok\.io)")


def find_tunnel_provider(requested: str = "auto", fake: bool = False) -> str:
    """Determine available tunnel provider or raise informative error."""
    if fake or requested == "fake":
        return "fake"

    req = requested.lower().strip()
    if req in ("cloudflared", "ngrok"):
        if shutil.which(req):
            return req
        msg = f"Requested tunnel provider '{req}' not found in PATH."
        raise RuntimeError(msg)

    if req == "auto":
        if shutil.which("cloudflared"):
            return "cloudflared"
        if shutil.which("ngrok"):
            return "ngrok"
        msg = (
            "Neither 'cloudflared' nor 'ngrok' found in PATH. Install cloudflared "
            "(e.g. 'brew install cloudflared') or ngrok, or use --fake for local/testing mode."
        )
        raise RuntimeError(msg)

    msg = (
        f"Unknown tunnel provider '{requested}'. Supported: 'auto', 'cloudflared', 'ngrok', 'fake'."
    )
    raise RuntimeError(msg)


def extract_tunnel_url(line: str, provider: str) -> str | None:
    """Extract public HTTPS tunnel URL from provider process log line."""
    if provider == "cloudflared":
        match = CLOUDFLARED_URL_REGEX.search(line)
        return match.group(0) if match else None
    if provider == "ngrok":
        match = NGROK_URL_REGEX.search(line)
        return match.group(0) if match else None
    return None


class TunnelSession:
    """Manages the webhook receiver server and associated tunnel subprocess."""

    def __init__(
        self,
        alert_source: PagerDutyAlertSource | None = None,
        host: str = "127.0.0.1",
        port: int = 9108,
        provider: str = "auto",
        fake: bool = False,
        secret: str | None = None,
        require_signature: bool = True,
        clock: Clock | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.fake = fake
        self.provider = find_tunnel_provider(requested=provider, fake=fake)
        self.alert_source = alert_source or PagerDutyAlertSource(clock=clock)
        self.server = WebhookReceiverServer(
            alert_source=self.alert_source,
            host=self.host,
            port=self.port,
            secret=secret,
            require_signature=require_signature,
            clock=clock,
        )
        self.proc: asyncio.subprocess.Process | None = None
        self.public_url: str | None = None
        self.webhook_url: str | None = None

    async def start(self, timeout_seconds: float = 30.0) -> str:
        """Start webhook server and tunnel subprocess, waiting for public URL."""
        logger = get_logger()
        await self.server.start()

        if self.provider == "fake":
            self.public_url = f"https://fake-tunnel-{self.port}.understudy.dev"
            self.webhook_url = f"{self.public_url}/webhook"
            logger.info("tunnel_started", provider="fake", public_url=self.public_url)
            return self.public_url

        if self.provider == "cloudflared":
            cmd = ["cloudflared", "tunnel", "--url", f"http://{self.host}:{self.port}"]
        else:
            cmd = ["ngrok", "http", str(self.port), "--log=stdout"]

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        scan_stream = self.proc.stderr if self.provider == "cloudflared" else self.proc.stdout
        if scan_stream is None:
            await self.stop()
            msg = f"Failed to attach to {self.provider} process stream"
            raise RuntimeError(msg)

        async def _scan_stream(stream: asyncio.StreamReader) -> str | None:
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    return None
                line = line_bytes.decode("utf-8", errors="replace")
                extracted = extract_tunnel_url(line, self.provider)
                if extracted:
                    return extracted

        url_task = asyncio.create_task(_scan_stream(scan_stream))

        try:
            public_url = await asyncio.wait_for(url_task, timeout=timeout_seconds)
        except TimeoutError as exc:
            await self.stop()
            msg = f"Timed out waiting for {self.provider} tunnel URL after {timeout_seconds}s"
            raise RuntimeError(msg) from exc

        if not public_url:
            await self.stop()
            msg = f"Failed to discover public URL from {self.provider} tunnel output"
            raise RuntimeError(msg)

        self.public_url = public_url
        self.webhook_url = f"{self.public_url}/webhook"
        logger.info(
            "tunnel_started",
            provider=self.provider,
            public_url=self.public_url,
            webhook_url=self.webhook_url,
        )
        return self.public_url

    async def stop(self) -> None:
        """Shut down tunnel subprocess and webhook server."""
        logger = get_logger()
        if self.proc:
            try:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=3.0)
                except TimeoutError:
                    self.proc.kill()
                    await self.proc.wait()
            except ProcessLookupError:
                pass
            finally:
                self.proc = None

        await self.server.stop()
        logger.info("tunnel_stopped", provider=self.provider)

    async def __aenter__(self) -> "TunnelSession":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()

    async def run_until_cancelled(self) -> None:
        """Run until cancelled, keeping server and tunnel active."""
        try:
            while True:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()
