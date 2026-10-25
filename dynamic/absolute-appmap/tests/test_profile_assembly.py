"""Profile assembly: build_profile, multi-run merge, names round trip."""
import json
import shutil

import pytest
import yaml

from appmap.profile import PROFILE_SCHEMA_VERSION, build_profile, multirun, names
from appmap.store import Store


def _copy_run(src, tmp_path, tag, run_id):
    dst = tmp_path / tag
    shutil.copytree(src, dst)
    meta = json.loads((dst / "run.json").read_text())
    meta["run_id"] = run_id
    (dst / "run.json").write_text(json.dumps(meta))
    return dst


def _sql(run, *stmts):
    store = Store(run / "appmap.sqlite")
    try:
        for s in stmts:
            store.conn.execute(s)
    finally:
        store.close()


@pytest.fixture(scope="module")
def ssr_profile(ssr_built_run):
    return build_profile([ssr_built_run])


def test_single_run_profile_shape(ssr_profile):
    p = ssr_profile
    assert p["profile_schema_version"] == PROFILE_SCHEMA_VERSION == "1.0.0"
    assert p["status"] == "profiled" and p["differences"] is None
    assert p["purpose"] is None and p["feature_names"] == {}
    assert len(p["environments"]) == 1
    assert {a["prefix"] for a in p["layout"]["areas"]} >= {"/projects", "/admin"}
    assert p["features"] and p["coverage"]["features_x_roles"]
    assert p["technologies"] and all(t["evidence"] for t in p["technologies"])
    t = p["technologies"][0]
    assert set(t["versions_by_environment"]) == {p["environments"][0]["run_id"]}
    json.dumps(p)  # serializable


def test_two_runs_differences_list_modified_route(ssr_built_run, tmp_path):
    other = _copy_run(ssr_built_run, tmp_path, "r2", "20990101-000000")
    host = Store.open_readonly(other / "appmap.sqlite").conn.execute(
        "SELECT host FROM routes LIMIT 1").fetchone()["host"]
    _sql(other, f"INSERT INTO routes(pattern, host, kind) VALUES('/staging-only', '{host}', 'page')",
         "UPDATE routes SET pattern='/renamed-admin' WHERE pattern='/admin'")
    p = build_profile([ssr_built_run, other])
    d = p["differences"]
    by = {r["pattern"]: r for r in d["routes"]}
    run1 = p["environments"][0]["run_id"]
    assert by["/staging-only"]["present_in"] == ["20990101-000000"]
    assert by["/admin"]["present_in"] == [run1]
    assert by["/renamed-admin"]["present_in"] == ["20990101-000000"]
    assert "/projects" not in by
    assert not any("overlap" in w for w in p["warnings"])
    assert len(p["environments"]) == 2
    # technologies joined by name+category, one entry each, versions per environment
    names_ = [(t["name"], t["category"]) for t in p["technologies"]]
    assert len(names_) == len(set(names_))
    assert set(p["technologies"][0]["versions_by_environment"]) == {run1, "20990101-000000"}


def test_overlap_warning_on_unrelated_runs(ssr_built_run, tmp_path):
    other = _copy_run(ssr_built_run, tmp_path, "unrelated", "20990202-000000")
    _sql(other, "DELETE FROM role_access", "DELETE FROM sources WHERE item_kind='route'",
         "UPDATE states SET route_pattern=NULL", "UPDATE forms SET action_endpoint_id=NULL",
         "DELETE FROM routes",
         "INSERT INTO routes(pattern, host, kind) VALUES('/zeta/one','h','page')",
         "INSERT INTO routes(pattern, host, kind) VALUES('/zeta/two','h','page')")
    p = build_profile([ssr_built_run, other])
    assert any("overlap" in w and "30%" in w for w in p["warnings"])


def test_unbuilt_run_is_partial_with_could_not_determine(ssr_built_run, tmp_path):
    run = _copy_run(ssr_built_run, tmp_path, "unbuilt", "20990303-000000")
    meta = json.loads((run / "run.json").read_text())
    meta["status"] = "in_progress"
    (run / "run.json").write_text(json.dumps(meta))
    _sql(run, "DELETE FROM flow_steps", "DELETE FROM flows", "DELETE FROM value_links",
         "UPDATE roles SET auth_ok=0 WHERE name='alice'")
    p = build_profile([run])
    assert p["status"] == "partial"
    text = json.dumps(p["could_not_determine"])
    assert "flows" in text and "alice" in text and "in_progress" in text
    assert all(c["run_id"] == "20990303-000000" for c in p["could_not_determine"])


def test_spa_profile_has_schema_free_warning_or_not(spa_built_run):
    p = build_profile([spa_built_run])
    assert p["status"] in ("profiled", "partial")
    has_schema = any(e["source"] in ("openapi", "graphql") for e in p["data_model"])
    assert has_schema or any("schema" in w for w in p["warnings"])


def test_production_masks_observed_unless_included(ssr_built_run, tmp_path):
    run = _copy_run(ssr_built_run, tmp_path, "prod", "20990404-000000")
    meta = json.loads((run / "run.json").read_text())
    meta["contains_production_data"] = True
    (run / "run.json").write_text(json.dumps(meta))
    masked = build_profile([run])
    plain = build_profile([run], include_observed_accounts=True)
    stage = build_profile([ssr_built_run])
    vals = lambda p: [o["value"] for o in p["accounts"]["observed"]]  # noqa: E731
    assert vals(plain) == vals(stage)
    if vals(stage):
        masked_vals = [v for v, s in zip(vals(masked), vals(stage)) if v != s]
        assert all("***" in v for v in masked_vals)


def test_merge_joins_by_key_and_warns_on_low_overlap():
    def run(rid, ver, routes, eps):
        return {"run_id": rid, "routes": routes,
                "endpoints": [{"method": m, "path_pattern": p} for m, p in eps],
                "technologies": [{"name": "Flask", "category": "framework", "versions": [ver],
                                  "confidence": "likely", "evidence": [{"ref": {}}]}],
                "roles": [{"name": "admin", "reachable_routes": len(routes),
                           "reachable_endpoints": len(eps)}]}
    out = multirun.merge([run("a", "2.0", ["/x", "/y"], [("GET", "/api/x")]),
                          run("b", "3.0", ["/x", "/z"], [("GET", "/api/x"), ("POST", "/api/x")])])
    tech = out["technologies"][0]
    assert tech["versions_by_environment"] == {"a": ["2.0"], "b": ["3.0"]}
    assert tech["versions"] == ["2.0", "3.0"]
    d = out["differences"]
    assert {r["pattern"] for r in d["routes"]} == {"/y", "/z"}
    assert [(e["method"], e["path_pattern"]) for e in d["endpoints"]] == [("POST", "/api/x")]
    assert d["technologies"][0]["name"] == "Flask"
    assert d["role_access"][0]["role"] == "admin"
    assert out["warnings"] == []  # 50% overlap
    low = multirun.merge([run("a", "2", ["/a", "/b", "/c", "/d"], []), run("b", "2", ["/a", "/q", "/r", "/s"], [])])
    assert any("overlap" in w for w in low["warnings"])


def test_names_survive_rerun_and_vanished_key_goes_stale(ssr_profile, tmp_path):
    path = tmp_path / "profile-names.yaml"
    names.emit_profile_names(ssr_profile, path)
    doc = yaml.safe_load(path.read_text())
    keys = [f["key"] for f in doc["features"]]
    assert keys == [f["key"] for f in ssr_profile["features"]]
    assert all(f["name"] == ft["name_suggestion"] and f["description"] == ""
               for f, ft in zip(doc["features"], ssr_profile["features"]))
    assert doc["stale"] == []
    doc["purpose"] = "Tracks projects."
    doc["features"][0].update(name="Project board", description="d", how_it_works="h")
    doc["features"].append({"key": "ft-gone", "name": "Old", "description": "x", "how_it_works": "y"})
    path.write_text(yaml.safe_dump(doc))
    names.emit_profile_names(ssr_profile, path)
    doc2 = yaml.safe_load(path.read_text())
    assert doc2["purpose"] == "Tracks projects."
    first = next(f for f in doc2["features"] if f["key"] == keys[0])
    assert first["name"] == "Project board" and first["how_it_works"] == "h"
    assert [s["key"] for s in doc2["stale"]] == ["ft-gone"]
    assert doc2["stale"][0]["name"] == "Old"
    names.emit_profile_names(ssr_profile, path)  # idempotent; stale kept
    assert [s["key"] for s in yaml.safe_load(path.read_text())["stale"]] == ["ft-gone"]
    # a stale key that returns is restored
    p2 = {**ssr_profile, "features": ssr_profile["features"] + [{"key": "ft-gone", "name_suggestion": "Gone"}]}
    names.emit_profile_names(p2, path)
    doc3 = yaml.safe_load(path.read_text())
    assert doc3["stale"] == []
    assert next(f for f in doc3["features"] if f["key"] == "ft-gone")["name"] == "Old"


def test_apply_names_fills_profile_and_ignores_malformed(ssr_profile, tmp_path):
    path = tmp_path / "n.yaml"
    k = ssr_profile["features"][0]["key"]
    path.write_text(yaml.safe_dump({
        "purpose": "Tracks projects.",
        "features": [{"key": k, "name": "Board", "description": "d", "how_it_works": "h"},
                     {"key": "ft-unknown", "name": "X"}, "junk", {"name": "no key"},
                     {"key": 5, "name": "n"}, {"key": ssr_profile["features"][-1]["key"],
                                               "name": ["bad"], "description": 3}],
        "stale": [{"key": "ft-old", "name": "Z"}]}))
    p = {**ssr_profile, "purpose": None, "feature_names": {}}
    assert names.apply_profile_names(p, path) == 1
    assert p["purpose"] == "Tracks projects."
    assert p["feature_names"] == {k: {"name": "Board", "description": "d", "how_it_works": "h"}}
    assert names.apply_profile_names(p, tmp_path / "missing.yaml") == 0
    (tmp_path / "bad.yaml").write_text(": : [")
    assert names.apply_profile_names(p, tmp_path / "bad.yaml") == 0


def test_unparseable_names_file_untouched_with_backup(ssr_profile, tmp_path):
    path = tmp_path / "profile-names.yaml"
    path.write_text("purpose: hand written\nfeatures: [unclosed\n")
    before = path.read_text()
    warns = names.emit_profile_names(ssr_profile, path)
    assert path.read_text() == before
    assert (tmp_path / "profile-names.yaml.bak").read_text() == before
    assert len(warns) == 1 and ".bak" in warns[0]
    assert names.apply_profile_names({**ssr_profile, "feature_names": {}}, path) == 0


def test_untouched_stub_vanishes_without_stale_noise(ssr_profile, tmp_path):
    path = tmp_path / "n.yaml"
    names.emit_profile_names(ssr_profile, path)
    doc = yaml.safe_load(path.read_text())
    stubs = [dict(f) for f in doc["features"]]
    one = {**ssr_profile["features"][0]}
    p_few = {**ssr_profile, "features": [one]}
    kept, renamed, described = stubs[1], stubs[2], stubs[3] if len(stubs) > 3 else None
    renamed["name"] = "My name"
    if described:
        described["description"] = "hand text"
    doc["features"] = [stubs[0], kept, renamed] + ([described] if described else [])
    path.write_text(yaml.safe_dump(doc))
    names.emit_profile_names(p_few, path)
    stale = {s["key"] for s in yaml.safe_load(path.read_text())["stale"]}
    assert renamed["key"] in stale and kept["key"] not in stale
    if described:
        assert described["key"] in stale


def test_malformed_entries_preserved_and_scalars_coerced(ssr_profile, tmp_path):
    path = tmp_path / "n.yaml"
    k = ssr_profile["features"][0]["key"]
    path.write_text(yaml.safe_dump({"purpose": "p", "features": [
        "junk", {"key": k, "name": 42, "description": ["list"]}, {"name": "no key"}]}))
    names.emit_profile_names(ssr_profile, path)
    feats = yaml.safe_load(path.read_text())["features"]
    assert "junk" in feats and {"name": "no key"} in feats
    assert {"key": k, "name": 42, "description": ["list"]} in feats  # uncoercible: verbatim
    path.write_text(yaml.safe_dump({"features": [{"key": k, "name": 42}]}))
    names.emit_profile_names(ssr_profile, path)
    assert next(f for f in yaml.safe_load(path.read_text())["features"]
                if f["key"] == k)["name"] == "42"


def test_key_in_both_features_and_stale_is_deduped(ssr_profile, tmp_path):
    path = tmp_path / "n.yaml"
    k = ssr_profile["features"][0]["key"]
    path.write_text(yaml.safe_dump({"features": [{"key": k, "name": "Live"}],
                                    "stale": [{"key": k, "name": "Old"}]}))
    names.emit_profile_names(ssr_profile, path)
    doc = yaml.safe_load(path.read_text())
    assert doc["stale"] == []
    assert [f["name"] for f in doc["features"] if f["key"] == k] == ["Live"]


def test_pending_frontier_makes_partial(ssr_built_run, tmp_path):
    run = _copy_run(ssr_built_run, tmp_path, "pend", "20990505-000000")
    _sql(run, "INSERT INTO frontier(role, kind, url, status) "
              "VALUES('admin','url','http://x/never','pending')")
    p = build_profile([run])
    assert p["status"] == "partial"
    assert any("crawl not finished: 1 pending frontier items" in c["reason"]
               for c in p["could_not_determine"])


def test_corrupt_run_json_and_broken_sections_do_not_raise(ssr_built_run, tmp_path):
    run = _copy_run(ssr_built_run, tmp_path, "broken", "20990606-000000")
    (run / "run.json").write_text("{not json")
    _sql(run, "DROP TABLE value_links", "DROP TABLE role_access")
    p = build_profile([run])
    assert p["status"] == "partial"
    items = {c["item"]: c["reason"] for c in p["could_not_determine"]}
    assert "run metadata" in items
    assert any("OperationalError" in r for r in items.values())
    assert any(i in items for i in ("layout", "coverage", "accounts"))
    assert p["environments"][0]["contains_production_data"] is True  # unknown -> masked


def test_missing_sqlite_is_an_error(tmp_path):
    (tmp_path / "empty-run").mkdir()
    with pytest.raises(FileNotFoundError):
        build_profile([tmp_path / "empty-run"])


def _seed_observed(run, value):
    _sql(run, "INSERT INTO exchanges(role, origin, method, url, status, content_type, "
              f"resp_body) VALUES('admin','ui','GET','http://x/api/me',200,'application/json',"
              f"'{{\"email\": \"{value}\"}}')")


def test_second_run_production_masks_observed_in_all_runs(ssr_built_run, tmp_path):
    first = _copy_run(ssr_built_run, tmp_path, "stg", "20990707-000000")
    second = _copy_run(ssr_built_run, tmp_path, "prod2", "20990708-000000")
    _seed_observed(first, "carol.stage@corp.example")
    meta = json.loads((second / "run.json").read_text())
    meta["contains_production_data"] = False
    meta["profile"] = "production"  # flag absent: profile alone marks production
    (second / "run.json").write_text(json.dumps(meta))
    masked = build_profile([first, second])
    vals = [o["value"] for o in masked["accounts"]["observed"]]
    assert "c***@corp.example" in vals and "carol.stage@corp.example" not in vals
    shown = build_profile([first, second], include_observed_accounts=True)
    assert "carol.stage@corp.example" in [o["value"] for o in shown["accounts"]["observed"]]
    assert masked["environments"][1]["contains_production_data"] is True


def test_feature_routes_and_endpoints_are_unique(ssr_built_run, spa_built_run):
    for run in (ssr_built_run, spa_built_run):
        for f in build_profile([run])["features"]:
            assert len(f["routes"]) == len(set(f["routes"])), f
            assert len(f["endpoints"]) == len(set(f["endpoints"])), f


def test_captured_schema_without_entities_warns_differently(spa_built_run):
    p = build_profile([spa_built_run])
    assert not any(e["source"] in ("openapi", "graphql") for e in p["data_model"])
    assert not any("no API schema" in w for w in p["warnings"])
    assert any("has no entity types" in w for w in p["warnings"])
