"""Session-scoped built runs of the fixture apps for the profile tests (ruling P2).

Each fixture runs the documented CLI sequence once (init -> auth -> probe passive -> crawl
loop -> probe active -> approvals -> build) via ``test_e2e.workflow`` and returns the run
directory. Registered for all tests by an import in ``conftest.py``.
"""
from pathlib import Path

import pytest

# Secrets each fixture server issued (sid cookies, CSRF tokens, JWTs), by built run directory,
# so profile tests can scan their outputs for them after the server has stopped.
ISSUED_SECRETS: dict[Path, set[str]] = {}


def _built(name: str, roles_for, created_sql: str, tmp_path_factory) -> Path:
    from test_e2e import Instrumented, workflow  # lazy: avoids a conftest import cycle

    server = Instrumented(name)
    try:
        res = workflow(server, tmp_path_factory.mktemp(f"profile-{name}"),
                       roles_for(server.url), created_sql)
        ISSUED_SECRETS[res["run"]] = set(server.secrets)
        return res["run"]
    finally:
        server.stop()


def _ssr_roles(url: str) -> list[dict]:
    login = {"type": "form", "url": f"{url}/login"}
    return [
        {"name": "admin", "login": {**login, "username_env": "APPMAP_ADMIN_USER",
                                    "password_env": "APPMAP_ADMIN_PASS"}},
        {"name": "alice", "login": {**login, "username_env": "APPMAP_ALICE_USER",
                                    "password_env": "APPMAP_ALICE_PASS"}},
        {"name": "anonymous", "login": {"type": "none"}},
    ]


def _spa_roles(url: str) -> list[dict]:
    return [{"name": n, "login": {"type": "form", "url": f"{url}/",
                                  "username_env": f"APPMAP_{n.upper()}_USER",
                                  "password_env": f"APPMAP_{n.upper()}_PASS",
                                  "success_selector": "text=Signed in as"}}
            for n in ("admin", "alice")] + [{"name": "anonymous", "login": {"type": "none"}}]


@pytest.fixture(scope="session")
def ssr_built_run(tmp_path_factory, fixture_credentials) -> Path:
    created = ("SELECT DISTINCT CAST(substr(json_extract(resp_headers, '$.location'),"
               " length('/projects/') + 1) AS INTEGER) AS id FROM exchanges "
               "WHERE method='POST' AND url LIKE '%/projects/new' AND status=302")
    return _built("ssr", _ssr_roles, created, tmp_path_factory)


@pytest.fixture(scope="session")
def spa_built_run(tmp_path_factory, fixture_credentials) -> Path:
    created = ("SELECT DISTINCT json_extract(resp_body, '$.id') AS id FROM exchanges "
               "WHERE method='POST' AND url LIKE '%/api/items' AND status=201")
    return _built("spa", _spa_roles, created, tmp_path_factory)
