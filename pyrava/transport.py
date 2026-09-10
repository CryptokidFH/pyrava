"""HTTP transport for Barava packets.

Confirmed against firmware: the device exposes a single endpoint,
``POST http://{ip}:8080/barava-host-post``, and takes real HTTP headers with
the packet as a raw body. `network.md` presents requests as a JSON object with
``headers`` and ``body`` keys, which reads as though a JSON envelope might be
expected; it is not. ``envelope="json"`` remains available in case a future
firmware changes this.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .errors import TransportError

__all__ = ["Transport", "HttpTransport", "Envelope", "DEFAULT_PORT", "DEFAULT_PATH"]

Envelope = Literal["raw", "json"]

#: Confirmed against hardware: the device exposes exactly one endpoint.
DEFAULT_PORT = 8080
DEFAULT_PATH = "/barava-host-post"


@dataclass
class Response:
    status: int
    text: str
    headers: dict[str, str]


class Transport:
    """Interface for anything that can move a packet to a device."""

    def send(self, headers: Mapping[str, Any], body: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass


class HttpTransport(Transport):
    """Blocking HTTP transport built on the standard library.

    Uses ``urllib`` so the package has no hard third-party dependency. If
    ``requests`` is installed it is used instead, which gives connection reuse
    and therefore noticeably lower latency at short ping intervals.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        path: str = DEFAULT_PATH,
        method: str = "POST",
        timeout: float = 5.0,
        envelope: Envelope = "raw",
        use_requests: bool | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.path = path if path.startswith("/") else "/" + path
        self.method = method.upper()
        self.timeout = float(timeout)
        self.envelope = envelope
        self._session = None

        if use_requests is None:
            use_requests = True
        if use_requests:
            try:
                import requests

                self._session = requests.Session()
            except ImportError:
                self._session = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"

    # -- payload shaping ---------------------------------------------------

    def _build(self, headers: Mapping[str, Any], body: str):
        clean = {str(k): str(v) for k, v in headers.items()}
        if self.envelope == "json":
            payload = json.dumps({"headers": clean, "body": body}).encode()
            return {"Content-Type": "application/json"}, payload
        # No Content-Type: the known-good request sends the packet as bare
        # bytes, and the ESP32 handler reads the body without negotiating.
        return clean, body.encode()

    # -- sending -----------------------------------------------------------

    def send(self, headers: Mapping[str, Any], body: str) -> str:
        wire_headers, payload = self._build(headers, body)
        if self._session is not None:
            return self._send_requests(wire_headers, payload)
        return self._send_urllib(wire_headers, payload)

    def _send_requests(self, headers: dict[str, str], payload: bytes) -> str:
        import requests

        try:
            resp = self._session.request(
                self.method, self.url, headers=headers, data=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TransportError(f"request to {self.url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TransportError(
                f"{self.url} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.text

    def _send_urllib(self, headers: dict[str, str], payload: bytes) -> str:
        request = urllib.request.Request(
            self.url, data=payload, headers=headers, method=self.method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise TransportError(
                f"{self.url} returned HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TransportError(f"request to {self.url} failed: {exc.reason}") from exc
        except OSError as exc:
            raise TransportError(f"request to {self.url} failed: {exc}") from exc

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
