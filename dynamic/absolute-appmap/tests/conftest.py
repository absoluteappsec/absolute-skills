import threading
from pathlib import Path

import pytest
from werkzeug.wrappers import Request, Response

from appmap.config import load_config

DATA = Path(__file__).parent / "data"


@pytest.fixture
def cfg():
    return load_config(DATA / "config_staging.yaml")


@pytest.fixture
def prod_cfg():
    return load_config(DATA / "config_prod.yaml")


class StubServer:
    """Local Werkzeug server on an ephemeral port; ``requests`` logs (method, path)."""

    def __init__(self, app):
        import socketserver
        from werkzeug.serving import ThreadedWSGIServer

        class _Server(ThreadedWSGIServer):
            def server_bind(self):  # skip HTTPServer's slow socket.getfqdn()
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = "127.0.0.1", self.socket.getsockname()[1]

        self.requests: list[tuple[str, str]] = []
        self._app = app
        self._srv = _Server("127.0.0.1", 0, self._wsgi)
        self.port = self._srv.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def _wsgi(self, environ, start_response):
        self.requests.append((environ["REQUEST_METHOD"], environ["PATH_INFO"]))
        return self._app(environ, start_response)

    def stop(self) -> None:
        self._srv.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture
def stub_server():
    """Factory: ``stub_server(routes_or_app)`` -> StubServer.

    ``routes_or_app`` is a Flask/WSGI app, or a dict mapping path -> handler. A handler
    takes a werkzeug ``Request`` and returns a ``Response`` (or str/bytes/tuple accepted
    by ``Response``). Servers are stopped at teardown.
    """
    servers: list[StubServer] = []

    def start(routes_or_app):
        if isinstance(routes_or_app, dict):
            routes = routes_or_app

            @Request.application
            def app(request):
                handler = routes.get(request.path)
                if handler is None:
                    return Response("not found", status=404)
                out = handler(request)
                return out if isinstance(out, Response) else Response(out)
        else:
            app = routes_or_app
        srv = StubServer(app)
        servers.append(srv)
        return srv

    yield start
    for s in servers:
        s.stop()
