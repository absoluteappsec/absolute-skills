import json

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


def test_encoded_variants_of_secret():
    import json as j
    from urllib.parse import quote, quote_plus
    pw = 'p@ss "word"!'
    r = Redactor([pw], [])
    for v in (pw, quote(pw, safe=""), quote_plus(pw), j.dumps(pw)[1:-1]):
        assert "word" not in r.text(f"x={v}&y=1") or v == "", v
        assert "<redacted:secret>" in r.text(f"x={v}")


def test_short_secret_does_not_corrupt_placeholders():
    r = Redactor(["ed", "hunter2"], [])
    out = r.text("Bearer abc hunter2 edge")
    assert out == "<redacted:authorization> <redacted:secret> <redacted:secret>ge"
    assert r.text("hunter2 ed") == "<redacted:secret> <redacted:secret>"


def test_har_shaped_name_value_lists():
    r = Redactor([], [])
    har = {"request": {
        "cookies": [{"name": "sid", "value": "abc", "path": "/"}],
        "headers": [{"name": "Authorization", "value": "Bearer x"},
                    {"name": "Cookie", "value": "sid=abc; t=d"},
                    {"name": "X-CSRF-Token", "value": "c"},
                    {"name": "Accept", "value": "text/html"}],
        "postData": {"params": [{"name": "password", "value": "p"}, {"name": "q", "value": "ok"}]}},
        "response": {"headers": [{"name": "Set-Cookie", "value": "sid=abc; Path=/"}]}}
    out = r.json(har)
    assert out["request"]["cookies"] == [{"name": "sid", "value": "<redacted:cookie>", "path": "/"}]
    h = {x["name"]: x["value"] for x in out["request"]["headers"]}
    assert h == {"Authorization": "<redacted:authorization>",
                 "Cookie": "sid=<redacted:cookie>; t=<redacted:cookie>",
                 "X-CSRF-Token": "<redacted:csrf>", "Accept": "text/html"}
    assert out["request"]["postData"]["params"] == [
        {"name": "password", "value": "<redacted:password>"}, {"name": "q", "value": "ok"}]
    assert out["response"]["headers"][0]["value"] == "sid=<redacted:cookie>; Path=/"


def test_json_non_str_values_scrubbed():
    r = Redactor(["hunter2"], [])
    from pathlib import Path
    out = r.json({"b": b"pw=hunter2", "e": Exception("Bearer abc"), "p": Path("/x/hunter2"), "n": 1})
    assert out == {"b": "pw=<redacted:secret>", "e": "<redacted:authorization>",
                   "p": "/x/<redacted:secret>", "n": 1}


def test_html_csrf_inputs_and_meta_redacted():
    """I10: opaque tokens in HTML (hidden inputs, meta tags) are not under a header or JSON
    key; they are scrubbed by attribute name, whatever the attribute order."""
    r = Redactor([], [])
    html = ('<meta name="csrf-token" content="Zx81kQ0aPq">'
            "<meta content='Ab12Cd34Ef' name='_xsrf'>"
            '<input type="hidden" value="tok-91827364" name="authenticity_token">'
            '<input type=hidden name=api_key value=k-5566778899>'
            '<input type="password" value="hunter22">'
            '<meta name="viewport" content="width=device-width">'
            '<input name="title" value="appmap-1 demo">')
    out = r.text(html)
    for secret in ("Zx81kQ0aPq", "Ab12Cd34Ef", "tok-91827364", "k-5566778899", "hunter22"):
        assert secret not in out
    assert '<redacted:csrf>' in out and '<redacted:secret>' in out
    assert 'content="width=device-width"' in out and 'value="appmap-1 demo"' in out


def test_learned_runtime_values_scrubbed_everywhere():
    r = Redactor([], [])
    assert r.text("session 6f1d2c3b4a59") == "session 6f1d2c3b4a59"
    r.learn_cookies("sid=6f1d2c3b4a59; theme=dark; lang=en")
    r.learn_cookies("remember=AAAA1111CCCC; Path=/; HttpOnly\nflag=1", set_cookie=True)
    assert not r.learn("short") and not r.learn("undefined")
    out = r.text("ids: 6f1d2c3b4a59 AAAA1111CCCC dark en 1")
    assert "6f1d2c3b4a59" not in out and "AAAA1111CCCC" not in out
    assert out.endswith("dark en 1")
    assert "6f1d2c3b4a59" not in json.dumps(r.json({"note": "token=6f1d2c3b4a59"}))
