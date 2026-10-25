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


@pytest.mark.parametrize("kind,label,url,method", [
    ("submit", "Submit", "http://x/taskManager/forgot_password/", "POST"),
    ("submit", "Submit", "http://x/accounts/forgotPassword", None),
    ("submit", "Send reset link", "http://x/pw", "POST"),
    ("request", "Go", "http://x/api/password_reset_request", "POST"),
    ("click", "Resend verification email", None, "POST"),
    ("click", "Resend activation e-mail", None, "POST"),
    ("submit", "Email me a magic link", "http://x/login", "POST"),
    ("submit", "Magic link", "http://x/auth/magic", "POST"),
    ("submit", "Forgot your password?", "http://x/reset", "POST"),
])
def test_account_recovery_submits_held_as_outbound(kind, label, url, method):
    f = classify(_a(label, method, "ui", kind=kind, url=url), "staging")
    assert f.held is True
    assert f.category == "outbound_message"


@pytest.mark.parametrize("kind,label,url,method", [
    ("click", "Forgot password?", "http://x/taskManager/forgot_password/", "GET"),
    ("click", "Forgot your password?", "http://x/forgot-password", None),
    ("request", "Go", "http://x/forgot_password/", "GET"),
])
def test_account_recovery_navigation_not_held(kind, label, url, method):
    assert classify(_a(label, method, "ui", kind=kind, url=url), "staging").held is False


def test_login_submit_with_forgot_link_in_context_not_held():
    f = classify(_a("Log in", "POST", "ui", kind="submit", url="http://x/login",
                    context="Forgot your password?"), "staging")
    assert f.held is False


# -- field fix 3: account-editing forms (fields passed by the crawler) ----------------
def _f(*names, types=None, options=None):
    out = []
    for n in names:
        d = {"name": n, "type": (types or {}).get(n, "text")}
        if options and n in options:
            d["options"] = options[n]
        out.append(d)
    return out


def _sub(label, url, fields, method="POST", origin="ui", kind="submit", context=""):
    return ActionDescriptor(kind=kind, method=method, url=url, label=label,
                            context_text=context, has_confirm=False, role="user",
                            origin=origin, fields=fields)


def test_categories_include_account_change():
    from appmap.destructive import CATEGORIES
    assert "account_change" in CATEGORIES


@pytest.mark.parametrize("fields,cat", [
    (_f("username", "email", "is_staff", "groups"), "account_change"),
    (_f("first_name", "is_superuser"), "account_change"),
    (_f("user_permissions"), "account_change"),
    (_f("role"), "account_change"),
    (_f("user[roles]"), "account_change"),
    (_f("username", "first_name"), "account_change"),
    (_f("email", "first_name"), "credential_change"),
    (_f("password1", "password2", types={"password1": "password", "password2": "password"}),
     "credential_change"),
    (_f("new_password"), "credential_change"),
])
def test_account_fields_held(fields, cat):
    f = classify(_sub("Save", "http://x/admin/auth/user/1/change/", fields), "staging")
    assert f.held is True and f.category == cat
    f = classify(_sub("Save", "http://x/settings", fields), "staging")
    assert f.held is True and f.category == cat
    assert any("field" in r for r in f.reasons)


def test_account_fields_held_even_cross_role():
    f = classify(_sub("Save", "http://x/users/3", _f("email", "is_staff"), origin="cross_role",
                      kind="request"), "staging")
    assert f.held is True and f.category == "account_change"


def test_display_name_form_not_held():
    f = classify(_sub("Save", "http://x/profile", _f("display_name", "bio")), "staging")
    assert f.held is False
    # a password-type input is not a credential by type alone (a PIN, VTM's ssn)
    f = classify(_sub("Save", "http://x/account/pin",
                      _f("pin", "ssn", types={"pin": "password"})), "staging")
    assert f.held is False


def test_get_form_with_account_fields_not_held():
    f = classify(_sub("Search", "http://x/admin/users", _f("username", "email"),
                      method="GET"), "staging")
    assert f.held is False


@pytest.mark.parametrize("label,url", [
    ("Log in", "http://x/login"), ("Sign in", "http://x/session"),
    ("Register", "http://x/accounts/new"), ("Create account", "http://x/u"),
    ("Submit", "http://x/taskManager/register/"), ("Sign up", "http://x/join"),
])
def test_login_and_registration_forms_not_held(label, url):
    fields = _f("username", "email", "password", types={"password": "password"})
    assert classify(_sub(label, url, fields), "staging").held is False


def test_registration_with_privilege_field_held():
    f = classify(_sub("Register", "http://x/register", _f("username", "is_staff")), "staging")
    assert f.held is True and f.category == "account_change"


@pytest.mark.parametrize("url", [
    "http://x/admin/auth/user/1/change/", "http://x/admin/auth/user/add/",
    "http://x/users/7/edit", "http://x/users/7/update", "http://x/accounts/7",
])
def test_account_admin_url_held_without_fields(url):
    f = classify(_sub("Save", url, []), "staging")
    assert f.held is True and f.category == "account_change"


def test_account_admin_delete_url_stays_delete():
    f = classify(_sub("Yes, I'm sure", "http://x/admin/auth/user/5/delete/", []), "staging")
    assert f.category == "delete"


def test_account_admin_url_get_not_held():
    f = classify(_a("chris", "GET", "ui", url="http://x/admin/auth/user/1/change/"), "staging")
    assert f.held is False


def test_bulk_action_with_delete_option_held_as_delete():
    fields = _f("action", "_selected_action", types={"action": "select",
                                                     "_selected_action": "checkbox"},
                options={"action": ["", "delete_selected"]})
    f = classify(_sub("Go", "http://x/admin/taskManager/project/", fields), "staging")
    assert f.held is True and f.category == "delete"


def test_bulk_action_on_user_changelist_held_as_account_change():
    fields = _f("action", "_selected_action", types={"action": "select",
                                                     "_selected_action": "checkbox"},
                options={"action": ["", "make_active"]})
    f = classify(_sub("Go", "http://x/admin/auth/user/", fields), "staging")
    assert f.held is True and f.category == "account_change"


def test_bulk_action_elsewhere_not_held():
    fields = _f("action", "index", types={"action": "select"},
                options={"action": ["", "mark_published"]})
    assert classify(_sub("Go", "http://x/admin/blog/post/", fields), "staging").held is False


def test_replayed_request_body_keys_are_fields():
    f = classify(ActionDescriptor("request", "POST", "http://x/api/x", "", 
                                  '{"email": "a@b", "is_staff": true}', False, "user",
                                  "replay"), "staging")
    assert f.held is True and f.category == "account_change"
    f = classify(ActionDescriptor("request", "POST", "http://x/api/x", "",
                                  "username=a&groups=1", False, "user", "cross_role"),
                 "staging")
    assert f.held is True and f.category == "account_change"
    f = classify(ActionDescriptor("request", "POST", "http://x/api/x", "",
                                  '{"title": "x"}', False, "user", "replay"), "staging")
    assert f.held is False


@pytest.mark.parametrize("label,method,url,held", [
    ("EMAIL ADDRESS", "GET", "http://x/admin/auth/user/?o=2.1", False),  # sort link
    ("Email settings", "GET", "http://x/settings/email", False),
    ("Notifications", "GET", "http://x/me", False),
    ("Email", "POST", "http://x/x", True),
    ("Send email", "GET", "http://x/x", True),
    ("Email", "GET", "http://x/api/sendEmail", True),
])
def test_mail_nouns_on_get_links_not_held(label, method, url, held):
    """Field fix 3 minor: a GET link labelled with a mail noun only navigates."""
    assert classify(_a(label, method, "ui", url=url), "staging").held is held
