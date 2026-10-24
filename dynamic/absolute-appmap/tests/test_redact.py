from appmap.redact import Redactor

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_nature-1"


def test_redacts_jwt_cookie_auth_and_known_secret():
    r = Redactor(["hunter2"], [])
    assert r.headers({"Authorization": "Bearer eyJa.eyJb.c"})["Authorization"] == "<redacted:authorization>"
    assert r.headers({"Cookie": "sid=abc; theme=dark"})["Cookie"] == "sid=<redacted:cookie>; theme=<redacted:cookie>"
    assert "hunter2" not in r.text("pw=hunter2")
    assert r.json({"password": "x", "name": "n"}) == {"password": "<redacted:password>", "name": "n"}


def test_customer_data_not_redacted():
    assert Redactor([], []).json({"email": "a@b.com"}) == {"email": "a@b.com"}


def test_set_cookie_and_other_headers():
    r = Redactor([], [])
    h = r.headers({"Set-Cookie": "sid=abc; Path=/; HttpOnly", "X-CSRF-Token": "t", "X-Api-Key": "k",
                   "Accept": "text/html", "Proxy-Authorization": "Basic x"})
    assert h["Set-Cookie"] == "sid=<redacted:cookie>; Path=/; HttpOnly"
    assert h["X-CSRF-Token"] == "<redacted:csrf>"
    assert h["X-Api-Key"] == "<redacted:api_key>"
    assert h["Proxy-Authorization"] == "<redacted:authorization>"
    assert h["Accept"] == "text/html"


def test_header_lookup_case_insensitive():
    assert Redactor([], []).headers({"authorization": "x"})["authorization"] == "<redacted:authorization>"


def test_text_redacts_jwt_and_bearer():
    r = Redactor([], [])
    assert r.text(f"token {JWT} end") == "token <redacted:jwt> end"
    assert r.text("Authorization: Bearer abc123") == "Authorization: <redacted:authorization>"


def test_json_nested_and_key_patterns():
    r = Redactor([], [])
    out = r.json({"a": [{"api_key": "k", "csrfToken": "c", "x-xsrf": "x", "pwd": "p",
                         "client_secret": "s", "name": "n"}], "n": 1, "none": None})
    assert out == {"a": [{"api_key": "<redacted:api_key>", "csrfToken": "<redacted:csrf>",
                          "x-xsrf": "<redacted:csrf>", "pwd": "<redacted:password>",
                          "client_secret": "<redacted:secret>", "name": "n"}], "n": 1, "none": None}


def test_json_string_values_scrubbed():
    r = Redactor(["hunter2"], [])
    assert r.json({"note": f"my hunter2 and {JWT}"}) == {"note": "my <redacted:secret> and <redacted:jwt>"}


def test_form():
    r = Redactor([], [])
    assert r.form({"username": "u", "password": "p", "csrf_token": "c"}) == {
        "username": "u", "password": "<redacted:password>", "csrf_token": "<redacted:csrf>"}


def test_url_query_params_redacted_and_secret_scrubbed():
    r = Redactor(["hunter2"], [])
    assert r.url("https://x.com/a?token=abc&q=1&pw=hunter2") == \
        "https://x.com/a?token=<redacted:secret>&q=1&pw=<redacted:secret>"
    assert r.url("https://x.com/a?api_key=k&password=p") == \
        "https://x.com/a?api_key=<redacted:api_key>&password=<redacted:password>"
    assert r.url("https://x.com/plain") == "https://x.com/plain"


def test_extra_patterns_and_empty_secrets_ignored():
    r = Redactor(["", "ab"], [r"ACME-\d+"])
    assert r.text("id ACME-123 here") == "id <redacted:secret> here"
    assert r.text("abc") == "<redacted:secret>c"  # short secrets still scrubbed; empty ignored
    assert Redactor([""], []).text("hello") == "hello"
