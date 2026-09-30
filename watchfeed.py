#!/usr/bin/env python3
"""
jobfeed-watchdog: detect silent breakage in job-board / data feeds.

A "feed" is a JSON or YAML endpoint that serves a list of job records (or any
list-of-records). This tool computes a lightweight structural fingerprint of
each feed and compares it against a stored baseline. When the schema changes,
the record count collapses, or the feed stops responding, it exits non-zero so
a GitHub Actions job fails and can page you (Slack / email / Discord).

Design goals:
  * stdlib-only core (urllib) — runs anywhere, no heavy installs in CI.
  * YAML support is optional (pyyaml) — JSON feeds need nothing extra.
  * deterministic, testable pure functions (no network in unit tests).

Exit codes:
  0  all feeds healthy (or no feeds configured)
  1  one or more feeds reported breakage (schema change / count drop / down)
  2  usage / configuration error
"""
import json
import math
import sys
import urllib.request
import urllib.error

try:
    import yaml  # optional
    _HAVE_YAML = True
except Exception:  # pragma: no cover - environment dependent
    yaml = None
    _HAVE_YAML = False


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_payload(text):
    """Parse a feed body into Python data. JSON is always supported; YAML is
    supported when pyyaml is installed. Raises ValueError on unparseable input."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty feed body")
    # Prefer JSON (strict). Fall back to YAML, which is a superset, for .yml.
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        if _HAVE_YAML:
            return yaml.safe_load(text)
        raise ValueError("feed is not valid JSON and pyyaml is not installed")


def extract_records(data, record_path=None):
    """Return the list of records to fingerprint.

    ``record_path`` is a dotted path into the parsed document (e.g.
    "results.jobs" or "data"). It may be omitted when the document IS the list,
    or when the first list-valued key is a clear "records" container.
    """
    if record_path:
        node = data
        for part in record_path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ValueError("record_path %r not found in feed" % record_path)
            node = node[part]
        return node

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        # Explicit, conventional containers first.
        for key in ("results", "records", "data", "jobs", "items", "payload"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        # Otherwise the first list-valued key, if there is exactly one obvious.
        lists = [(k, v) for k, v in data.items() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0][1]
    raise ValueError("could not locate a list of records in feed")


# --------------------------------------------------------------------------- #
# Fingerprinting
# --------------------------------------------------------------------------- #
def record_key_set(records, sample=25):
    """Union of keys across up to ``sample`` dict records (order-independent)."""
    keys = set()
    for rec in records[:sample]:
        if isinstance(rec, dict):
            keys.update(rec.keys())
    return keys


def top_level_shape(data):
    """Stable description of the document's top-level container + record key
    set. Used to detect structural (schema) changes."""
    if isinstance(data, list):
        return ("list", record_key_set(data))
    if isinstance(data, dict):
        return ("dict", frozenset(data.keys()))
    return ("scalar", type(data).__name__)


def fingerprint(data, record_path=None):
    """Return a compact, hashable description of a feed:
    {top: shape, keys: [...], count: N}"""
    shape = top_level_shape(data)
    try:
        records = extract_records(data, record_path)
        if not isinstance(records, list):
            # A mis-configured record_path (or a feed whose records container
            # is not a list, e.g. it resolves to a dict/scalar) used to crash
            # record_key_set on `records[:sample]` (slicing a non-sequence).
            # The documented contract is to degrade to an empty snapshot
            # (count=0, keys=[]) — the same fallback used when no records are
            # located — so a library/PyPI user gets a well-formed fingerprint,
            # never a traceback, on a bad record_path.
            records = []
        count = len(records)
        keys = sorted(record_key_set(records))
    except ValueError:
        count = 0
        keys = []
    return {"top": shape[0], "keys": keys, "count": count}


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def compare(baseline, current, count_ratio=0.5, min_count=0):
    """Compare two fingerprints. Returns a list of human-readable problems.

    count_ratio:  current.count must be >= baseline.count * count_ratio
                  (guards against "feed returns a handful of records after a
                  parser break" — the classic silent failure).
    min_count:    absolute floor; a feed that legitimately had 0 records before
                  must not be flagged for still having 0.
    """
    problems = []
    if baseline is None:
        # No baseline yet: this run establishes it. Only flag an empty feed.
        if current["count"] == 0:
            problems.append("no baseline and current feed has 0 records")
        return problems

    if baseline["top"] != current["top"]:
        problems.append(
            "top-level container changed: %s -> %s" % (baseline["top"], current["top"])
        )

    if baseline["keys"] and current["keys"]:
        missing = [k for k in baseline["keys"] if k not in current["keys"]]
        added = [k for k in current["keys"] if k not in baseline["keys"]]
        if missing:
            problems.append("records missing keys (possibly removed): %s" % ", ".join(missing))
        if added:
            problems.append("records added keys: %s" % ", ".join(added))

    b_count = baseline["count"]
    c_count = current["count"]
    if b_count > 0:
        # ceil (not int/truncate): current must be >= baseline*ratio. A
        # collapse strictly below the ratio floor must be flagged even on
        # small feeds (e.g. 3 -> 1 at ratio 0.5). int() truncates toward
        # zero and silently lets the collapse through.
        floor = max(min_count, math.ceil(b_count * count_ratio))
        if c_count < floor:
            problems.append(
                "record count dropped: %d -> %d (floor %d)" % (b_count, c_count, floor)
            )
    if c_count == 0 and b_count > 0:
        problems.append("feed returned 0 records (was %d)" % b_count)

    return problems


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def fetch(url, timeout=30, headers=None):
    """GET a URL and return the response body text. Raises on HTTP/network
    errors so the caller can treat them as breakage."""
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "jobfeed-watchdog/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def load_config(path):
    """Load feeds.json or feeds.yml. Each feed:
    {id, url, record_path?, headers?, timeout?, count_ratio?, min_count?}"""
    text = open(path).read()
    if path.endswith((".yml", ".yaml")):
        if not _HAVE_YAML:
            raise ValueError("feeds.yml requires pyyaml (pip install pyyaml) or use feeds.json")
        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)
    if isinstance(raw, dict) and "feeds" in raw:
        feeds = raw["feeds"]
    elif isinstance(raw, list):
        feeds = raw
    else:
        raise ValueError("config must be a list of feeds or {feeds: [...]}")
    for f in feeds:
        if "url" not in f or "id" not in f:
            raise ValueError("each feed needs 'id' and 'url'")
    return feeds


def _baseline_path(state_dir, feed_id):
    return "%s/%s.baseline.json" % (state_dir, feed_id)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    if len(argv) < 2:
        print("usage: watchfeed.py <config.json|config.yml> <state_dir>", file=sys.stderr)
        return 2

    config_path, state_dir = argv[0], argv[1]
    try:
        feeds = load_config(config_path)
    except Exception as e:
        print("config error: %s" % e, file=sys.stderr)
        return 2

    import os
    os.makedirs(state_dir, exist_ok=True)

    any_problem = False
    report = []
    for feed in feeds:
        fid = feed["id"]
        url = feed["url"]
        count_ratio = float(feed.get("count_ratio", 0.5))
        min_count = int(feed.get("min_count", 0))
        timeout = int(feed.get("timeout", 30))
        headers = feed.get("headers")
        record_path = feed.get("record_path")

        entry = {"id": fid, "url": url, "ok": True, "problems": []}
        try:
            body = fetch(url, timeout=timeout, headers=headers)
            data = parse_payload(body)
            current = fingerprint(data, record_path)
        except Exception as e:
            entry["ok"] = False
            entry["problems"].append("fetch/parse failed: %s" % e)
            entry["count"] = None
            report.append(entry)
            any_problem = True
            continue

        entry["count"] = current["count"]
        bpath = _baseline_path(state_dir, fid)
        baseline = None
        if os.path.exists(bpath):
            try:
                baseline = json.load(open(bpath))
            except Exception:
                baseline = None

        problems = compare(baseline, current, count_ratio=count_ratio, min_count=min_count)
        entry["problems"] = problems
        entry["ok"] = not problems
        if problems:
            any_problem = True

        # Persist the current snapshot as the new baseline ONLY when the feed
        # is healthy. Overwriting the baseline with a broken snapshot would make
        # the next run compare broken-to-broken and report "all healthy",
        # silently absorbing the regression this tool exists to catch. Holding
        # the last known-good baseline keeps the run red until the feed recovers
        # (or a human commits a new baseline to accept a genuine schema change).
        if entry["ok"]:
            with open(bpath, "w") as fh:
                json.dump(current, fh, sort_keys=True, indent=2)

        report.append(entry)

    # Machine-readable report on stdout (Actions can log this).
    print(json.dumps(report, indent=2, sort_keys=True))

    if any_problem:
        print("RESULT: BREAKAGE DETECTED", file=sys.stderr)
        return 1
    print("RESULT: ALL HEALTHY", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
