# chap-models-checker

Run `chap eval` against every repo in
[`github.com/chap-models`](https://github.com/chap-models) and produce a
status report. Used to keep an eye on which models still work end-to-end
against the current `chap-core`.

## Current status

As of the last full sweep (2026-05-11T09:45Z): **25 pass / 4 fail** of 29
chap-models repos. The 25 passes split into 11 fully clean and 14 that
only succeed because the sweep host pre-pulled their docker image with
`--platform=linux/amd64` (the model authors haven't published multi-arch
images; those 14 would fail on a pure arm64 deploy).

### Failing (4)

| Repo | Failure |
| --- | --- |
| [`Exponential_smoothing_state_space_model`](https://github.com/chap-models/Exponential_smoothing_state_space_model) | `docker_image_missing_runtime` — image lacks `forecast`; repo also has deeper rot beyond an image swap. No tracking issue (the original [#2](https://github.com/chap-models/Exponential_smoothing_state_space_model/issues/2) was scoped wrong and closed). |
| [`ewars_per_district`](https://github.com/chap-models/ewars_per_district) | `docker_pull_failed` — PR [#1](https://github.com/chap-models/ewars_per_district/pull/1) (chapkit-r-inla swap + predict.R `idx.pred` filter) open |
| [`minimal_template_example`](https://github.com/chap-models/minimal_template_example) | `model_runtime_error` — PR [#2](https://github.com/chap-models/minimal_template_example/pull/2) open |
| [`Vietnam-dengue-superensemble`](https://github.com/chap-models/Vietnam-dengue-superensemble) | `prediction_length` — `max_prediction_length=1`, chap-core wraps to extend to 3; INLA crashes (`inla.inlaprogram.has.crashed`) inside the iterative predict. Regression after upstream commits 2026-05-06T13:20Z (was green on prior sweep with same bundled dataset). |

### Passing only with `--platform=linux/amd64` (14)

These either pin an amd64-only base in their Dockerfile (chapkit-r-inla
ships INLA x86_64 binaries only) or never built an arm64 manifest in the
first place. Author should publish a multi-arch image, or the deploy
environment needs to force `linux/amd64` too.

| Repo | Style |
| --- | --- |
| [`auto_arima`](https://github.com/chap-models/auto_arima) | mlproject |
| [`auto_ets`](https://github.com/chap-models/auto_ets) | mlproject |
| [`baseline_model_for_sim_study`](https://github.com/chap-models/baseline_model_for_sim_study) | mlproject |
| [`chap_auto_ewars`](https://github.com/chap-models/chap_auto_ewars) | mlproject |
| [`chap_auto_ewars_weekly`](https://github.com/chap-models/chap_auto_ewars_weekly) | mlproject |
| [`D-FENSE---LNCC-ARp-2025-1`](https://github.com/chap-models/D-FENSE---LNCC-ARp-2025-1) | mlproject |
| [`epidemiar_example_model`](https://github.com/chap-models/epidemiar_example_model) | mlproject |
| [`ewars_template`](https://github.com/chap-models/ewars_template) | mlproject |
| [`INLA_baseline_model`](https://github.com/chap-models/INLA_baseline_model) | mlproject |
| [`LaCiD-UFRN-ARIMAX`](https://github.com/chap-models/LaCiD-UFRN-ARIMAX) | mlproject |
| [`Madagascar_ARIMA`](https://github.com/chap-models/Madagascar_ARIMA) | mlproject |
| [`mean`](https://github.com/chap-models/mean) | mlproject |
| [`rwanda_sarimax`](https://github.com/chap-models/rwanda_sarimax) | mlproject |
| [`XGBoost_for_Malawi`](https://github.com/chap-models/XGBoost_for_Malawi) | mlproject |

### Passing cleanly on the host's native arch (11)

| Repo | Style |
| --- | --- |
| [`auto_arima_chapkit`](https://github.com/chap-models/auto_arima_chapkit) | chapkit |
| [`chap_pymc`](https://github.com/chap-models/chap_pymc) | mlproject |
| [`chapkit_ewars_model`](https://github.com/chap-models/chapkit_ewars_model) | chapkit |
| [`chapkit_minimalist_example_py`](https://github.com/chap-models/chapkit_minimalist_example_py) | chapkit |
| [`chapkit_minimalist_example_r`](https://github.com/chap-models/chapkit_minimalist_example_r) | chapkit |
| [`chapkit_rwanda_malaria_bym_model`](https://github.com/chap-models/chapkit_rwanda_malaria_bym_model) | chapkit |
| [`chapkit_simple_multistep_model`](https://github.com/chap-models/chapkit_simple_multistep_model) | chapkit |
| [`chtorch`](https://github.com/chap-models/chtorch) | mlproject |
| [`rwanda_random_forest`](https://github.com/chap-models/rwanda_random_forest) | mlproject |
| [`Xiang_LSTM`](https://github.com/chap-models/Xiang_LSTM) | mlproject |
| [`Xiang_SVM`](https://github.com/chap-models/Xiang_SVM) | mlproject |

---

See [`STATUS.md`](STATUS.md) for the same tables plus per-repo
investigation notes and the forward-looking roadmap. The raw machine-
readable snapshot lives in [`last_report.json`](last_report.json).

This section is hand-updated and can drift. Refresh after a sweep with
`make run` (or `make reclassify` to re-render from existing logs without
rerunning chap eval).

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
   and surface the headline tables in [`STATUS.md`](STATUS.md).

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
| `STATUS.md` | Hand-maintained companion to the snapshot. Three sections: the headline pass/fail tables (refreshed from `last_report.json`), per-repo investigation notes, and the forward-looking roadmap. |
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
