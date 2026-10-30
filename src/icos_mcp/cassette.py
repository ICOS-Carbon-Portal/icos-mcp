"""Recorded proxy answers, so the contract tests do not depend on today's data.

``ICOS_CASSETTE=<dir>`` routes every proxy request through this transport:

* ``ICOS_CASSETTE_MODE=record`` asks the real proxy and writes each answer
  to ``<dir>/<sha1 of method and URL>.json``;
* anything else (the default) REPLAYS: a request with no recording fails
  loudly, naming its URL, instead of reaching the network.

A recording is the status, the content type and the body, nothing else.
The same recordings are what the M9 port (``mcpd``) will be replayed over,
so both servers see byte-identical proxy answers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

import httpx


class Cassette(httpx.AsyncBaseTransport):
    def __init__(self, directory: str, record: bool):
        self.dir = directory
        self.record = record
        self.inner = httpx.AsyncHTTPTransport() if record else None
        os.makedirs(directory, exist_ok=True)

    def _path(self, request: httpx.Request) -> str:
        key = hashlib.sha1(f"{request.method} {request.url}".encode()).hexdigest()
        return os.path.join(self.dir, f"{key}.json")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = self._path(request)
        if self.record:
            r = await self.inner.handle_async_request(request)
            _write(
                path,
                {
                    "url": str(request.url),
                    "status": r.status_code,
                    "content_type": r.headers.get("content-type", ""),
                    "body": base64.b64encode(await r.aread()).decode(),
                },
            )
        if not os.path.isfile(path):
            raise httpx.ConnectError(
                f"not recorded: {request.method} {request.url}", request=request
            )
        rec = _read(path)
        return httpx.Response(
            rec["status"],
            headers={"content-type": rec["content_type"]},
            content=base64.b64decode(rec["body"]),
            request=request,
        )

    async def aclose(self) -> None:
        if self.inner:
            await self.inner.aclose()


# small files, test-time only: plain blocking I/O is the honest choice
def _write(path: str, rec: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def transport() -> Cassette | None:
    d = os.environ.get("ICOS_CASSETTE", "")
    if not d:
        return None
    return Cassette(d, os.environ.get("ICOS_CASSETTE_MODE", "") == "record")
