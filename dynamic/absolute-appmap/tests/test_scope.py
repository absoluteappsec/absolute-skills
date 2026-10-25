from appmap.config import Scope
from appmap.scope import ScopeGuard


def guard(**kw):
    base = dict(allow_hosts=["app.example.com"], allow_path_prefixes=["/"],
                deny_patterns=["*/logout"])
    base.update(kw)
    return ScopeGuard(Scope(**base))


def test_deny_pattern_blocks_logout():
    g = guard()
    assert not g.allows("https://app.example.com/app/logout")
    assert g.allows("https://app.example.com/app/home")


def test_other_host_out_of_scope():
    assert not guard().allows("https://evil.example.org/")


def test_host_case_and_port_insensitive():
    assert guard().allows("https://APP.Example.com:8443/x")


def test_scheme_and_prefix():
    g = guard(allow_path_prefixes=["/app"])
    assert not g.allows("ftp://app.example.com/app")
    assert not g.allows("javascript:alert(1)")
    assert g.allows("http://app.example.com/app/x")
    assert not g.allows("http://app.example.com/other")


import pytest


@pytest.mark.parametrize("path", [
    "/app/logout/", "/app/%6Cogout", "/app/%256Cogout", "/app/logout;x=1",
    "/app/LOGOUT", "/app/x/../logout", "/app/logout/.", "/app\\logout",
])
def test_deny_bypass_forms_blocked(path):
    assert not guard(allow_path_prefixes=["/app"]).allows("https://app.example.com" + path)


@pytest.mark.parametrize("path", ["/app/x/../../admin", "/application", "/app/%2e%2e/admin"])
def test_prefix_bypass_forms_blocked(path):
    assert not guard(allow_path_prefixes=["/app"]).allows("https://app.example.com" + path)


def test_prefix_root_and_exact():
    g = guard(allow_path_prefixes=["/app/"])
    assert g.allows("https://app.example.com/app")
    assert g.allows("https://app.example.com/app/")


def test_login_and_static_hosts_only_in_their_context():
    """I2: an external IdP is reachable only from a login flow, a CDN only for static
    subresources; neither is ever in scope for crawling or probing."""
    g = guard(login_allow_hosts=["idp.example.net"], static_allow_hosts=["cdn.example.net"])
    assert not g.allows("https://idp.example.net/authorize")
    assert g.allows("https://idp.example.net/authorize", login=True)
    assert not g.allows("https://idp.example.net/authorize", static=True)
    assert not g.allows("https://cdn.example.net/app.js")
    assert g.allows("https://cdn.example.net/app.js", static=True)
    assert not g.allows("https://cdn.example.net/app.js", login=True)
    assert not g.allows("https://idp.example.net/logout", login=True)  # deny still applies
    assert not g.allows("ftp://idp.example.net/x", login=True)
