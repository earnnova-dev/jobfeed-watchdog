# jobfeed-watchdog

A tiny, dependency-light **GitHub Action template** that watches a job feed (or
any JSON/YAML endpoint that returns a list of records) and **fails the build**
when the feed silently breaks — schema change, record-count collapse, or
outage. No server, no billing, no secrets required.

## Why

Job-board operators and feed consumers discover silent breakage too late: a
parser change returns a handful of records, or a required field disappears, and
nothing pages anyone. This runs a structural fingerprint on every schedule and
turns the Actions run **red** when something regresses.

## What it checks, per feed

| Signal | What it catches |
|---|---|
| Top-level container type change | `list` -> `dict` (or vice-versa) |
| Record key set diff | a required field removed/renamed |
| Record-count floor (`count_ratio`, `min_count`) | "parser broke, returns 5 of 300" |
| Fetch / parse / HTTP error | feed down or malformed |

## Install (30 seconds)

```bash
git clone https://github.com/earnnova-dev/jobfeed-watchdog
cd jobfeed-watchdog
mkdir -p /path/to/your/repo/.github/workflows
cp watchfeed.py feeds.json /path/to/your/repo
cp .github/workflows/jobfeed-watchdog.yml /path/to/your/repo/.github/workflows/
```

Then edit `feeds.json` in your repo to list the feeds you want watched:

```json
{
  "feeds": [
    {
      "id": "my-job-feed",
      "url": "https://example.com/api/jobs",
      "record_path": "results",
      "count_ratio": 0.5,
      "min_count": 5,
      "timeout": 30,
      "headers": { "Authorization": "Bearer ***" }
    }
  ]
}
```

- `record_path` (optional): dotted path to the list of records, e.g. `results.jobs`.
  Omit when the response *is* the list, or when it's under a conventional key
  (`results`/`records`/`data`/`jobs`/`items`/`payload`).
- `count_ratio` (default `0.5`): current count must be ≥ baseline × ratio.
- `min_count` (default `0`): absolute floor.
- `headers` (optional): auth, etc.

## How it works

1. On each schedule (`0 */6 * * *` = every 6h, or `workflow_dispatch` for a
   manual run), `watchfeed.py` fetches each feed and computes a fingerprint:
   `{top-level container, sorted record key set, record count}`.
2. It compares against the last-known-good baseline stored in
   `state/<id>.baseline.json` (committed to your repo, so runs are reproducible
   and you can `git diff` exactly what changed).
3. If any feed reports a problem, the step exits non-zero → the run turns red
   → your normal Actions notification (email/Slack/Discord from repo settings)
   fires.
4. Only when healthy does it commit the new baseline — so it never "blesses"
   a broken feed as the new normal.

## Run it locally

```bash
python3 watchfeed.py feeds.json /tmp/state     # JSON feed: no deps
python3 watchfeed.py feeds.yml  /tmp/state     # YAML feed: needs `pip install pyyaml`
python3 -m pytest tests/                        # run the offline test suite
```

Exit codes: `0` all healthy · `1` breakage detected · `2` config/usage error.

## Design

- **stdlib-only core** (`urllib`): runs in any CI runner with no heavy install.
- **YAML optional**: JSON feeds need nothing extra; pyyaml only if a feed is YAML.
- **deterministic + offline-testable**: the fingerprint/compare functions have no
  network dependency, covered by `tests/`.

## Example report

```json
[
  {
    "id": "my-job-feed",
    "url": "https://example.com/api/jobs",
    "ok": false,
    "count": 3,
    "problems": [
      "record count dropped: 300 -> 3 (floor 150)",
      "records missing keys (possibly removed): salary, location"
    ]
  }
]
```

## License

MIT — use it anywhere. See [LICENSE](LICENSE).
