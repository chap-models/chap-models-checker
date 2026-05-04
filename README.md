# chap-models-checker

Run `chap eval` against every repo in
[`github.com/chap-models`](https://github.com/chap-models) and produce a
status report. Used to keep an eye on which models still work end-to-end
against the current `chap-core`.

## What it does

For each public repo under the `chap-models` org:

1. **Discover** via the GitHub org API (paginated, unauthenticated).
2. **Classify** as `mlproject` (MLproject file at the repo root) or
   `chapkit` (`pyproject.toml` lists `chapkit` as a dep).
3. **Pick a dataset** — curated `laos_subset.csv` /
   `nicaragua_weekly_subset.csv` from chap-core when their period +
   covariates match; synthetic CSV (+ optional sibling `.geojson`) otherwise.
4. **Run `chap eval`** via `uvx --from chap-core chap eval ...`. For
   chapkit-style repos, the model service is booted in a sibling Docker
   container (auto-builds the model's Dockerfile) or via `uv run` for
   pure-Python services with no R sources.
5. **Bucket the failure** when one happens
   (`docker_pull_failed`, `invalid_mlproject`, `schema_mismatch`,
   `model_runtime_error`, `nonzero_exit`, …) and write a per-repo
   suggested fix.
6. **Persist** results into `last_report.json` (commit-friendly snapshot)
   and `findings.md` (markdown table of the same data).

## Quick start

```bash
make install          # uv sync (one-time)
make run              # full sweep across all chap-models repos
make list             # show last-known status without running anything
make reclassify       # re-bucket failures from existing run.log files
```

Subcommand details:

```bash
uv run chap-models-checker run --help
uv run chap-models-checker run --repo auto_ets       # filtered, merges into snapshot
uv run chap-models-checker run --only-style chapkit  # chapkit-only sweep
uv run chap-models-checker run --spec-only           # classify + extract spec, skip chap eval
```

## Outputs

| File | Purpose |
| --- | --- |
| `last_report.json` | Commit-friendly snapshot of the most recent sweep — pass/fail, durations, failure category, suggestion, run.log path, dataset path, per-row `checked_at` timestamps. |
| `findings.md` | Markdown table of the same snapshot. Re-rendered from `last_report.json` whenever the sweep / `reclassify` runs. |
| `TRIAGE.md` | Hand-written companion. Captures investigation notes, root-cause analysis, PR links, and follow-up work that the auto-generated `findings.md` can't see. Durable across snapshot refreshes. |
| `work/<repo>/run.log` | Full `chap eval` output for each repo (stdout + stderr). Inspected by `reclassify` for re-bucketing. |
| `work/<repo>/eval.nc` | NetCDF prediction output written by `chap eval` on success. |

## Filtered runs and snapshot merging

`run --repo X` only re-tests the named repo(s). The result is merged into
the existing `last_report.json` instead of overwriting it, so untouched
rows keep their previous timestamps. This is what makes `list` show
honest staleness — the title bar reports the **oldest** per-row
`checked_at`, not the most recent merge.

A full sweep (`run` with no filter) replaces the snapshot wholesale and
flushes any stale rows for repos that no longer exist in the org list
(e.g. after a rename).

## Discovery filtering

`SKIPPED_REPOS` in `src/chap_models_checker/discover.py` lists repos
that live under `chap-models/` but aren't models themselves (currently
just this repo). They're filtered out before the sweep starts.

## Dev

```bash
make check        # ruff lint + format check + mypy + pyright (CI-equivalent)
make test         # pytest -q
make lint         # ruff format + auto-fix
```

The Makefile wraps `uv run chap-models-checker <cmd>` and the standard
ruff/mypy/pyright/pytest invocations — no shell glue needed.

Layout:

```
src/chap_models_checker/
├── cli.py              # typer entry point, subcommands, snapshot merge
├── discover.py         # GitHub org listing
├── spec.py             # MLproject / chapkit /info -> ModelSpec
├── chapkit_service.py  # boot a chapkit service from a clone
├── datagen.py          # curated lookup + synthetic CSV / geojson
├── runner.py           # build chap eval cmd, run + classify failures
├── report.py           # console panels + markdown rendering
└── models.py           # pydantic schemas (ModelSpec, RunResult, Report, …)
```

## License

[GPL-3.0](LICENSE), matching sister chap-models repos.
