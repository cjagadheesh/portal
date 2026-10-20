"""A fake EC2 metadata service on loopback, so the agent's IMDSv2 code runs for real."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = "tok-123"  # noqa: S105 - a fake, for a fake service


class FakeImds:
    def __init__(self) -> None:
        self.paths: dict[str, str] = {}
        self.token_requests = 0
        self.requests: list[tuple[str, str, str | None]] = []  # (method, path, token header)
        self.reject_next_token = False  # answer one GET with 401, as if the token went stale

    def server(self) -> ThreadingHTTPServer:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _reply(self, code: int, body: str = "") -> None:
                self.send_response(code)
                self.end_headers()
                self.wfile.write(body.encode())

            def do_PUT(self) -> None:
                fake.requests.append(("PUT", self.path, None))
                if self.path != "/latest/api/token" or not self.headers.get(
                    "X-aws-ec2-metadata-token-ttl-seconds"
                ):
                    return self._reply(400)
                fake.token_requests += 1
                self._reply(200, TOKEN)

            def do_GET(self) -> None:
                token = self.headers.get("X-aws-ec2-metadata-token")
                fake.requests.append(("GET", self.path, token))
                if token != TOKEN or fake.reject_next_token:
                    fake.reject_next_token = False
                    return self._reply(401)  # IMDSv2: no valid token, no data
                if self.path in fake.paths:
                    return self._reply(200, fake.paths[self.path])
                self._reply(404)

        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


@contextmanager
def running() -> Iterator[tuple[FakeImds, str]]:
    fake = FakeImds()
    server = fake.server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
