import pytest

from appmap.normalize import normalize_path


@pytest.mark.parametrize("path,known,marker,expected", [
    ("/users/42", [], None, "/users/{id}"),
    ("/o/3f2a9c1e-1b2c-4d5e-8f90-123456789abc", [], None, "/o/{uuid}"),
    ("/p/appmap-r1-x", [], "appmap-r1", "/p/{slug}"),
    ("/items/7", ["/items/:itemId"], None, "/items/{itemId}"),
    ("/items/7", ["/items/[itemId]"], None, "/items/{itemId}"),
    ("/items/7", ["/items/{itemId}"], None, "/items/{itemId}"),
    ("/f/0123456789abcdef01", [], None, "/f/{hash}"),
    ("/users/42?x=1", [], None, "/users/{id}"),
    ("/static/app", [], None, "/static/app"),
])
def test_normalize(path, known, marker, expected):
    assert normalize_path(path, known, marker=marker) == expected
