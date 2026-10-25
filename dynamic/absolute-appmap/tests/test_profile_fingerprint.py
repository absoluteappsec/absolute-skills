"""Profile technology fingerprinting: rule table + detection engine."""
import json
from pathlib import Path

import pytest
import yaml

from appmap.profile.evidence import Evidence, _cookie_names
from appmap.profile.fingerprint import CATEGORIES, Rule, Technology, detect, load_rules

DATA = Path(__file__).parent / "data" / "fingerprints"
VENDOR = Path(__file__).parent / "fixtures" / "apps" / "spa" / "static" / "vendor"
EXPECTED = yaml.safe_load((DATA / "expected.yaml").read_text())


def _ref(name, i=0):
    return {"run_id": "sample", "kind": "file", "id": f"{name}#{i}"}


def _ev(kind, text, name="x", i=0, headers=None):
    return Evidence("sample", kind, _ref(name, i), f"http://app.test/{name}", text, headers or {})


def sample_evidence(name: str) -> list[Evidence]:
    """Evidence records for a captured sample, shaped like ``iter_evidence`` output."""
    raw = (DATA / name).read_text()
    if name.endswith(".headers.json"):
        headers, cookies = {}, []
        for k, v in json.loads(raw).items():
            if k.lower() == "set-cookie":
                cookies = _cookie_names(v)
            else:
                headers[k] = v
        out = [_ev("header", "\n".join(f"{k}: {v}" for k, v in headers.items()), name,
                   headers=headers)]
        return out + [_ev("cookie", c, name, i + 1) for i, c in enumerate(cookies)]
    for suffix, kind in ((".error.html", "error_page"), (".html", "html"), (".js", "script"),
                         (".map", "source_map"), (".schema.json", "schema"),
                         (".schema.yaml", "schema"), (".auth.txt", "auth")):
        if name.endswith(suffix):
            return [_ev(kind, raw, name)]
    raise AssertionError(f"unknown sample type: {name}")


def _summary(techs: list[Technology]) -> set[tuple]:
    out = set()
    for t in techs:
        v = None if not t.versions else t.versions[0] if len(t.versions) == 1 else tuple(t.versions)
        out.add((t.name, v, t.confidence))
    return out


@pytest.fixture(scope="module")
def rules():
    return load_rules()


def test_rules_load_and_validate(rules, tmp_path):
    assert rules and all(isinstance(r, Rule) for r in rules)
    assert {r.category for r in rules} <= set(CATEGORIES)
    assert {r.confidence for r in rules} <= {"confirmed", "likely"}
    for r in rules:
        assert r.version is None or r.version.groups == 1, r.tech
    names = {r.tech for r in rules}
    for tech in ("Django", "Django REST Framework", "drf-spectacular", "Flask", "Werkzeug",
                 "Ruby on Rails", "Laravel", "Express", "Spring Boot", "ASP.NET", "PHP", "nginx",
                 "Apache HTTP Server", "IIS", "gunicorn", "uvicorn", "Cloudflare",
                 "Amazon CloudFront", "Fastly", "React", "Vue", "vue-router", "Angular",
                 "Next.js", "Nuxt", "SvelteKit", "jQuery", "Bootstrap", "Font Awesome",
                 "Google Analytics", "Google Tag Manager", "JWT bearer", "Cookie session",
                 "CSRF token", "OpenAPI", "GraphQL"):
        assert tech in names, tech

    base = {"tech": "X", "category": "js_library", "evidence": ["script"], "match": "x",
            "version": None, "confidence": "likely", "implies": None}
    bad = [{"category": "nope"}, {"match": "("}, {"version": "x(\\d)(\\d)"},
           {"evidence": ["cookie", "telepathy"]}, {"confidence": "guessed"},
           {"implies": [{"tech": "Y", "category": "nope"}]}]
    for i, patch in enumerate(bad):
        p = tmp_path / f"bad{i}.yaml"
        p.write_text(yaml.safe_dump({"rules": [{**base, **patch}]}))
        with pytest.raises(ValueError):
            load_rules(p)
    ok = tmp_path / "ok.yaml"
    ok.write_text(yaml.safe_dump({"rules": [base]}))
    assert [r.tech for r in load_rules(ok)] == ["X"]


@pytest.mark.parametrize("sample", sorted(EXPECTED))
def test_sample(sample, rules):
    expected = {(t, tuple(v) if isinstance(v, list) else v, c) for t, v, c in EXPECTED[sample]}
    assert _summary(detect(sample_evidence(sample), rules)) == expected


def test_every_sample_has_expectations():
    samples = {p.name for p in DATA.iterdir() if p.name != "expected.yaml"}
    assert samples == set(EXPECTED)


def test_two_rules_confirm(rules):
    one = detect([_ev("cookie", "csrftoken", "a", 0), _ev("cookie", "sessionid", "b", 1)], rules)
    django = next(t for t in one if t.name == "Django")
    assert django.confidence == "likely"  # one cookie rule, however many hits
    html = '<form method="post"><input type="hidden" name="csrfmiddlewaretoken" value="x"></form>'
    two = detect([_ev("cookie", "csrftoken", "a", 0), _ev("html", html, "c", 2)], rules)
    django = next(t for t in two if t.name == "Django")
    assert django.confidence == "confirmed"
    assert {e["kind"] for e in django.evidence} == {"cookie", "html"}


def test_implies_language_likely(rules):
    techs = detect([_ev("error_page", "<html><body><h1>Whitelabel Error Page</h1></body></html>"),
                    _ev("cookie", "csrftoken", "c", 1)], rules)
    py = next(t for t in techs if t.name == "Python")
    assert (py.category, py.confidence, py.versions) == ("language", "likely", [])
    assert py.evidence and py.evidence[0]["ref"] == _ref("c", 1)
    java = next(t for t in techs if t.name == "Java")
    assert java.confidence == "likely"


def test_evidence_entries_capped_and_redacted(rules):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
    evs = [_ev("header", f"X-Powered-By: Express trace={jwt}{'a' * 400}", "h", i)
           for i in range(6)]
    express = next(t for t in detect(evs, rules) if t.name == "Express")
    assert len(express.evidence) == 3
    for e in express.evidence:
        assert set(e) == {"ref", "kind", "snippet"} and e["kind"] == "header"
        assert len(e["snippet"]) <= 200 and jwt not in e["snippet"]
        assert "Express" in e["snippet"]


def test_conflicting_versions_listed_sorted(rules):
    techs = detect([_ev("header", "Server: nginx/1.25.3", "a", 0),
                    _ev("header", "Server: nginx/1.9.10", "b", 1),
                    _ev("header", "Server: nginx/1.25.3", "c", 2)], rules)
    assert next(t for t in techs if t.name == "nginx").versions == ["1.9.10", "1.25.3"]


def test_no_false_version_from_app_code(rules):
    bundle = ('import Vue from "vue";import $ from "jquery";const version = "2"; var v="1.2";'
              'console.log("jQuery v" + v, "Bootstrap v" + version, React.version);'
              'export const meta = {version: "4.4.3", name: "vue-router"};')
    techs = detect([_ev("script", bundle, "app.js")], rules)
    for t in techs:
        if t.category in ("js_library", "frontend_framework", "css_framework"):
            assert t.versions == [], t


def test_vue_fixture_banner(rules):
    evs = [_ev("script", (VENDOR / f).read_text(), f, i)
           for i, f in enumerate(("vue.global.prod.js", "vue-router.global.prod.js"))]
    techs = {t.name: t for t in detect(evs, rules)}
    assert (techs["Vue"].versions, techs["Vue"].confidence) == (["3.4.38"], "confirmed")
    assert techs["Vue"].category == "frontend_framework"
    assert (techs["vue-router"].versions, techs["vue-router"].confidence) == (["4.4.3"],
                                                                              "confirmed")


def test_detect_time_bounded_on_capped_bodies(rules):
    import time
    html = ('<div class="row"><a href="/p/1">x</a><script src="/static/app.js"></script>'
            '<link rel="stylesheet" href="/s.css"></div>\n') * 20000
    js = ('function a(b){return b&&b.c?"x":void 0};var v="1.2",version="2";\n') * 30000
    evs = [_ev("html", html[:2 * 1024 * 1024]), _ev("script", js[:2 * 1024 * 1024]),
           _ev("error_page", html[:2 * 1024 * 1024]), _ev("source_map", js[:2 * 1024 * 1024])]
    t = time.monotonic()
    assert detect(evs, rules) == []
    assert time.monotonic() - t < 1.0
