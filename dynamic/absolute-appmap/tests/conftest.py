import threading
from pathlib import Path

import pytest
from werkzeug.wrappers import Request, Response

from appmap.config import RunConfig, load_config

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


# --- Task 8: fixture applications, config factory, browser -------------------------

import importlib.util
import os
import urllib.request

import yaml

from appmap.rundir import create_run_dir

APPS = Path(__file__).parent / "fixtures" / "apps"
FIXTURE_ENV = {
    "APPMAP_ADMIN_USER": "admin", "APPMAP_ADMIN_PASS": "admin-pw-Fx1",
    "APPMAP_ALICE_USER": "alice", "APPMAP_ALICE_PASS": "alice-pw-Fx2",
}


@pytest.fixture(scope="session", autouse=True)
def fixture_credentials():
    """Fixture-app credentials in the environment (monkeypatch is not session-scoped)."""
    saved = {k: os.environ.get(k) for k in FIXTURE_ENV}
    os.environ.update(FIXTURE_ENV)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _load_app(name: str):
    spec = importlib.util.spec_from_file_location(
        f"fixture_app_{name}", APPS / name / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.create_app()


def _post(url: str) -> None:
    urllib.request.urlopen(urllib.request.Request(url, method="POST", data=b""),
                           timeout=5).read()


@pytest.fixture(scope="session")
def _ssr_server():
    srv = StubServer(_load_app("ssr"))
    yield srv
    srv.stop()


@pytest.fixture(scope="session")
def _spa_server():
    srv = StubServer(_load_app("spa"))
    yield srv
    srv.stop()


@pytest.fixture
def ssr_url(_ssr_server):
    """Base URL of the SSR fixture app (one server per session, state reset per test)."""
    _post(_ssr_server.url + "/__reset")
    return _ssr_server.url


@pytest.fixture
def spa_url(_spa_server):
    """Base URL of the Vue SPA fixture app (one server per session, state reset per test)."""
    _post(_spa_server.url + "/__reset")
    return _spa_server.url


def default_roles(base_url: str) -> list[dict]:
    return [
        {"name": "admin", "login": {"type": "form", "url": f"{base_url}/login",
                                    "username_env": "APPMAP_ADMIN_USER",
                                    "password_env": "APPMAP_ADMIN_PASS"}},
        {"name": "alice", "login": {"type": "form", "url": f"{base_url}/login",
                                    "username_env": "APPMAP_ALICE_USER",
                                    "password_env": "APPMAP_ALICE_PASS"}},
        {"name": "anonymous", "login": {"type": "none"}},
    ]


@pytest.fixture
def make_cfg(tmp_path):
    """Factory: ``make_cfg(base_url, roles=None, profile="staging", **overrides)``.

    ``roles`` is a list of role dicts in config-file shape (default: admin, alice,
    anonymous). ``overrides`` are extra top-level config keys (limits=..., spa=...).
    """
    counter = iter(range(1_000_000))

    def make(base_url: str, roles: list[dict] | None = None,
             profile: str = "staging", **overrides) -> RunConfig:
        raw = {
            "target": {"base_urls": [base_url]},
            "authorization": {"confirmed_by": "test"},
            "profile": profile,
            "roles": roles if roles is not None else default_roles(base_url),
            **overrides,
        }
        path = tmp_path / f"cfg-{next(counter)}.yaml"
        path.write_text(yaml.safe_dump(raw))
        return load_config(path)

    return make


@pytest.fixture
def run_dir():
    """Factory: ``run_dir(tmp_path, cfg) -> RunDir``."""
    return lambda tmp_path, cfg: create_run_dir(cfg, tmp_path)


@pytest.fixture(scope="session")
async def browser():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        yield b
        await b.close()


# Session-scoped built fixture-app runs shared by the profile tests (ruling P2).
from profile_helpers import spa_built_run, ssr_built_run  # noqa: E402,F401
