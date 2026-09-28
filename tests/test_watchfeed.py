"""Offline unit tests for the pure functions in watchfeed.py.
No network. Run: python3 -m pytest tests/  (or: python3 tests/test_watchfeed.py)"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watchfeed as wf


def test_parse_json():
    assert wf.parse_payload('{"a": [1, 2]}') == {"a": [1, 2]}


def test_parse_yaml_if_available():
    if not wf._HAVE_YAML:
        return  # skip if pyyaml absent (JSON is the required path)
    assert wf.parse_payload("a:\n  - 1\n  - 2\n") == {"a": [1, 2]}


def test_parse_empty_raises():
    try:
        wf.parse_payload("")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_extract_records_top_list():
    assert wf.extract_records([{"id": 1}, {"id": 2}]) == [{"id": 1}, {"id": 2}]


def test_extract_records_conventional_key():
    assert wf.extract_records({"jobs": [{"a": 1}]}) == [{"a": 1}]
    assert wf.extract_records({"results": [1, 2]}) == [1, 2]


def test_extract_records_record_path():
    data = {"data": {"items": [{"x": 1}]}}
    assert wf.extract_records(data, "data.items") == [{"x": 1}]


def test_extract_records_missing_path_raises():
    try:
        wf.extract_records({"a": 1}, "b.c")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_fingerprint_counts_and_keys():
    data = {"jobs": [{"id": 1, "title": "a"}, {"id": 2, "title": "b", "extra": 3}]}
    fp = wf.fingerprint(data)
    assert fp["count"] == 2
    assert fp["top"] == "dict"
    assert "title" in fp["keys"] and "id" in fp["keys"]


def test_compare_healthy():
    a = {"top": "dict", "keys": ["id", "title"], "count": 50}
    b = {"top": "dict", "keys": ["id", "title"], "count": 48}
    assert wf.compare(a, b) == []


def test_compare_no_baseline():
    # Establishing baseline; non-empty is fine.
    assert wf.compare(None, {"top": "dict", "keys": ["id"], "count": 10}) == []
    # No baseline + empty feed is flagged.
    assert wf.compare(None, {"top": "dict", "keys": [], "count": 0}) != []


def test_compare_schema_change():
    a = {"top": "dict", "keys": ["id", "title", "salary"], "count": 50}
    b = {"top": "dict", "keys": ["id", "title"], "count": 50}
    problems = wf.compare(a, b)
    assert any("missing keys" in p and "salary" in p for p in problems)


def test_compare_count_drop():
    a = {"top": "dict", "keys": ["id"], "count": 100}
    b = {"top": "dict", "keys": ["id"], "count": 20}  # below 0.5 floor
    problems = wf.compare(a, b)
    assert any("count dropped" in p for p in problems)


def test_compare_count_drop_floor_respects_ratio():
    a = {"top": "dict", "keys": ["id"], "count": 100}
    b = {"top": "dict", "keys": ["id"], "count": 60}  # above 0.5 floor
    assert wf.compare(a, b) == []


def test_compare_zero_out_from_positive():
    a = {"top": "dict", "keys": ["id"], "count": 10}
    b = {"top": "dict", "keys": ["id"], "count": 0}
    problems = wf.compare(a, b)
    assert any("0 records" in p for p in problems)


def test_compare_zero_to_zero_not_flagged():
    a = {"top": "dict", "keys": [], "count": 0}
    b = {"top": "dict", "keys": [], "count": 0}
    assert wf.compare(a, b) == []


def test_load_config_json():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"feeds": [{"id": "x", "url": "https://e.x"}]}, f)
        path = f.name
    feeds = wf.load_config(path)
    assert feeds[0]["id"] == "x"
    os.unlink(path)


def test_load_config_list_json():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump([{"id": "a", "url": "https://a"}], f)
        path = f.name
    assert wf.load_config(path)[0]["id"] == "a"
    os.unlink(path)


def test_broken_feed_not_blessed_as_baseline():
    """Regression guard for the core value prop: a broken feed must keep the
    last known-good baseline so the run STAYS red until the feed recovers.
    (Pre-fix, the baseline was overwritten with the broken snapshot, so the
    second run reported 'all healthy' and the tool went silent mid-incident.)
    Offline: monkeypatch fetch, no network."""
    state = tempfile.mkdtemp()
    cfg = os.path.join(state, "cfg.json")
    with open(cfg, "w") as f:
        json.dump({"feeds": [{"id": "f", "url": "http://x/",
                              "record_path": "jobs", "count_ratio": 0.5}]}, f)
    healthy = json.dumps({"jobs": [{"id": i, "title": "J%d" % i,
                                    "salary": 100 + i, "loc": "OSLO"}
                                   for i in range(50)]})
    broken = json.dumps({"jobs": [{"id": i, "title": "J%d" % i} for i in range(3)]})
    body = {"v": healthy}
    real_fetch = wf.fetch
    wf.fetch = lambda url, timeout=30, headers=None: body["v"]
    try:
        # 1) establish a healthy baseline
        assert wf.main([cfg, state]) == 0
        bl = json.load(open(os.path.join(state, "f.baseline.json")))
        assert bl["count"] == 50
        # 2) feed breaks: must alarm AND must NOT bless the broken snapshot
        body["v"] = broken
        assert wf.main([cfg, state]) == 1
        bl = json.load(open(os.path.join(state, "f.baseline.json")))
        assert bl["count"] == 50, "broken snapshot was blessed as new baseline"
        # 3) still broken: must STILL alarm (silent absorption is the bug)
        assert wf.main([cfg, state]) == 1
        bl = json.load(open(os.path.join(state, "f.baseline.json")))
        assert bl["count"] == 50
    finally:
        wf.fetch = real_fetch


if __name__ == "__main__":
    # Minimal runner so it works without pytest installed.
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as e:
            failed += 1
            print("FAIL", fn.__name__, "->", e)
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
