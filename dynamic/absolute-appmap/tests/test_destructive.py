import pytest

from appmap.destructive import ActionDescriptor, classify


def _a(label, method, origin, kind="click", url=None, context="", confirm=False):
    return ActionDescriptor(kind=kind, method=method, url=url, label=label, context_text=context,
                            has_confirm=confirm, role="admin", origin=origin)


@pytest.mark.parametrize("label,method,origin,profile,held,cat", [
    ("Delete project", "POST", "ui", "staging", True, "delete"),
    ("Save", "DELETE", "replay", "staging", True, "delete"),
    ("Sign out", "GET", "ui", "staging", True, "logout"),
    ("Change password", "POST", "ui", "staging", True, "credential_change"),
    ("Save", "POST", "ui", "staging", False, None),
    ("Save", "POST", "ui", "production", True, "data_create"),
    ("Save", "PUT", "cross_role", "production", False, None),
    ("Remove", "DELETE", "cross_role", "staging", True, "delete"),
    ("Pay now", "POST", "ui", "staging", True, "payment"),
    ("Invite user", "POST", "ui", "staging", True, "outbound_message"),
    ("Deactivate account", "POST", "ui", "staging", True, "deactivate"),
    ("Cancel", "GET", "ui", "staging", False, None),
    ("Dashboard", "GET", "ui", "production", False, None),
])
def test_classify(label, method, origin, profile, held, cat):
    f = classify(_a(label, method, origin), profile)
    assert f.held is held
    assert f.category == cat
    assert bool(f.reasons) == held


def test_logout_by_url():
    f = classify(_a("Go", "GET", "ui", url="http://x/logout"), "staging")
    assert f.category == "logout"


def test_confirm_dialog_is_delete_unless_other_category():
    assert classify(_a("Go", "POST", "ui", confirm=True), "staging").category == "delete"
    assert classify(_a("Pay", "POST", "ui", confirm=True), "staging").category == "payment"


def test_production_submit_is_data_create():
    f = classify(_a("Go", None, "ui", kind="submit"), "production")
    assert f.category == "data_create"
    assert classify(_a("Go", None, "approved", kind="submit"), "production").held is False
