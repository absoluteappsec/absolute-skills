import re
from datetime import date

from appmap.browser.forms import FieldInfo
from appmap.datagen import DataGen, hint_from_validation

MARKER = "appmap-20261024-120000"


def f(name="x", type="text", widget="input", **kw):
    return FieldInfo(name=name, type=type, widget=widget, **kw)


def test_value_respects_pattern_and_marker():
    gen = DataGen(MARKER, {})
    v = gen.value_for(f("name", pattern="[a-z0-9-]{3,40}", required=True), 1)
    assert re.fullmatch("[a-z0-9-]{3,40}", v) and v.startswith(MARKER)


def test_pattern_that_cannot_hold_marker_still_matches():
    v = DataGen(MARKER, {}).value_for(f("code", pattern=r"\d{4}"), 1)
    assert re.fullmatch(r"\d{4}", v)


def test_type_based_values():
    g = DataGen("appmap-t", {})
    assert g.value_for(f("e", "email"), 1) == "appmap-t@example.test"
    assert g.value_for(f("p", "tel"), 1) == "+15555550100"
    assert g.value_for(f("u", "url"), 1) == "https://example.test/appmap-t"
    assert g.value_for(f("n", "number", min="10", max="20"), 1) == "15"
    assert g.value_for(f("n", "number"), 1) == "1"
    assert g.value_for(f("d", "date", "date"), 1) == date.today().isoformat()
    assert g.value_for(f("s", "select", "select", options=["", "a", "b"]), 1) == "a"
    assert g.value_for(f("m", "select", "multiselect", options=["a", "b"]), 1) == ["a"]
    assert g.value_for(f("c", "checkbox", "checkbox"), 1) is True
    assert g.value_for(f("r", "radio", "radio", options=["x", "y"]), 1) == "x"
    assert g.value_for(f("t", "textarea", "textarea"), 1) == "appmap-t"


def test_maxlength_trims_marker():
    assert DataGen(MARKER, {}).value_for(f("t", maxlength=10), 1) == MARKER[:10]


def test_fixture_override():
    gen = DataGen("appmap-t", {"fields": {"^e-?mail$": "fixed@corp.test", "Phone": "{marker}-1"}})
    assert gen.value_for(f("email", "email"), 1) == "fixed@corp.test"
    assert gen.value_for(f("x", label="Phone number"), 1) == "appmap-t-1"


def test_file_sample_by_accept():
    g = DataGen("m", {})
    assert g.value_for(f("a", "file", "file", options=["image/png"]), 1).suffix == ".png"
    assert g.value_for(f("a", "file", "file", options=[".pdf"]), 1).suffix == ".pdf"
    assert g.value_for(f("a", "file", "file"), 1).exists()


def test_hints():
    assert hint_from_validation("Name must be at most 12 characters") == "max:12"
    assert hint_from_validation("Name must be 3-40 characters") == "len:3-40"
    assert hint_from_validation("Too short (minimum is 5 characters)") == "min:5"
    assert hint_from_validation("Please enter a valid email address") == "email"
    assert hint_from_validation("Please match the requested format") == "pattern"
    assert hint_from_validation("Something odd") is None


def test_hint_truncates_and_keeps_pattern():
    gen = DataGen(MARKER, {})
    fld = f("name", pattern="[a-z0-9-]{3,40}")
    v = gen.value_for(fld, 2, "max:12")
    assert v == MARKER[:12] and re.fullmatch("[a-z0-9-]{3,40}", v)
    assert len(gen.value_for(f("n"), 2, "min:30")) >= 30


def test_anchored_pattern_generation():
    v = DataGen(MARKER, {}).value_for(f("code", pattern=r"^\d{4}$"), 1)
    assert re.fullmatch(r"\d{4}", v)
    v = DataGen(MARKER, {}).value_for(f("n", pattern="^[a-z0-9-]{3,40}$"), 1)
    assert v == MARKER


def test_more_than_is_max_not_min():
    assert hint_from_validation("Must be no more than 20 characters") == "max:20"
    assert hint_from_validation("Use more than 20 characters") == "max:20"
    assert hint_from_validation("Please enter no fewer than 5 characters") == "min:5"
