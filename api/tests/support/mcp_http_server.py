"""Run a FastMCP app over real streamable HTTP in a dedicated thread.

The mcp/uvicorn stack misbehaves when servers are started and stopped
repeatedly inside the test session's event loop (from the third server
on, responses never complete and the suite hangs). A thread with its own
loop isolates the server lifecycle completely; clients connect over TCP
so cross-loop is a non-issue.
"""

import socket
import threading
import time

import uvicorn


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ThreadedMcpServer:
    """One FastMCP server on 127.0.0.1:<random port>, in its own thread."""

    def __init__(self, mcp, path: str = "/mcp"):
        self.port = free_port()
        self.path = path
        app = mcp.http_app(path=path, stateless_http=True)
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                log_level="error",
                timeout_graceful_shutdown=2,
            )
        )
        # uvicorn skips signal-handler installation off the main thread.
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path}"

    def __enter__(self) -> "ThreadedMcpServer":
        self._thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("MCP HTTP server failed to start within 10s")

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
