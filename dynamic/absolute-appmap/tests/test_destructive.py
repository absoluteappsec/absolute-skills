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


@pytest.mark.parametrize("label,url,method,cat", [
    ("deleteUser", None, "POST", "delete"),
    ("Deleted", None, "POST", "delete"),
    ("Removes", None, "POST", "delete"),
    ("Go", "http://x/api/deleteAccount", "POST", "delete"),
    ("Go", "http://x/api/sendEmail", "POST", "outbound_message"),
    ("Go", "http://x/users/sign_out", "GET", "logout"),
    ("Go", "http://x/log_out", "GET", "logout"),
    ("signOut", None, "GET", "logout"),
    ("Cancel order", None, "POST", "deactivate"),
    ("Cancel my booking", None, "POST", "deactivate"),
    ("Erase data", None, "POST", "delete"),
    ("Trash", None, "POST", "delete"),
    ("Archive", None, "POST", "delete"),
    ("Terminate session", None, "POST", "deactivate"),
    ("Close account", None, "POST", "deactivate"),
    ("Go", "http://x/api/resetPassword", "POST", "credential_change"),
])
def test_inflected_and_camel_case(label, url, method, cat):
    f = classify(_a(label, method, "ui", url=url), "staging")
    assert f.category == cat


@pytest.mark.parametrize("label,url", [
    ("Save", "http://x/api/payload"), ("Cancel", None), ("Resend", None), ("Settings", None),
    ("Go", "http://x/api/sentry"), ("Closet", None),
])
def test_no_false_positives(label, url):
    assert classify(_a(label, "POST", "ui", url=url), "staging").held is False
