# chap-models status

Single source of truth for the chap-models org sweep. Combines what
used to be three files:

- **Snapshot** — the headline pass/fail tables of the most recent sweep
  (was `findings.md`).
- **Per-repo notes** — hand-written investigation log for non-trivial
  failures (was `TRIAGE.md`).
- **Roadmap** — what's next, ordered by where the work has to happen
  (was `ROADMAP.md`).

`last_report.json` is the raw machine-readable snapshot the Snapshot
section below is rendered from. `make run` runs the full sweep and then
refreshes the Snapshot block automatically; `make reclassify` re-buckets
existing run logs and also refreshes the block. `make render-status`
refreshes only the block, without touching the JSON.

---

## Snapshot

<!-- BEGIN-SNAPSHOT -->

As of the last full sweep (**2026-05-18T13:10:02+00:00**): **27 pass / 1 fail** of 28 chap-models repos.

### Failing (1)

| Repo | Failure |
| --- | --- |
| [`Vietnam-dengue-superensemble`](https://github.com/chap-models/Vietnam-dengue-superensemble) | `prediction_length` |

### Passing only with `--platform=linux/amd64` (15)

These either pin an amd64-only base in their Dockerfile (chapkit-r-inla ships INLA x86_64 binaries only) or never built an arm64 manifest in the first place. Author should publish a multi-arch image, or the deploy environment needs to force `linux/amd64` too.

| Repo | Style |
| --- | --- |
| [`auto_arima`](https://github.com/chap-models/auto_arima) | mlproject |
| [`auto_ets`](https://github.com/chap-models/auto_ets) | mlproject |
| [`baseline_model_for_sim_study`](https://github.com/chap-models/baseline_model_for_sim_study) | mlproject |
| [`chap_auto_ewars`](https://github.com/chap-models/chap_auto_ewars) | mlproject |
| [`chap_auto_ewars_weekly`](https://github.com/chap-models/chap_auto_ewars_weekly) | mlproject |
| [`D-FENSE---LNCC-ARp-2025-1`](https://github.com/chap-models/D-FENSE---LNCC-ARp-2025-1) | mlproject |
| [`epidemiar_example_model`](https://github.com/chap-models/epidemiar_example_model) | mlproject |
| [`ewars_per_district`](https://github.com/chap-models/ewars_per_district) | mlproject |
| [`ewars_template`](https://github.com/chap-models/ewars_template) | mlproject |
| [`INLA_baseline_model`](https://github.com/chap-models/INLA_baseline_model) | mlproject |
| [`LaCiD-UFRN-ARIMAX`](https://github.com/chap-models/LaCiD-UFRN-ARIMAX) | mlproject |
| [`Madagascar_ARIMA`](https://github.com/chap-models/Madagascar_ARIMA) | mlproject |
| [`mean`](https://github.com/chap-models/mean) | mlproject |
| [`rwanda_sarimax`](https://github.com/chap-models/rwanda_sarimax) | mlproject |
| [`XGBoost_for_Malawi`](https://github.com/chap-models/XGBoost_for_Malawi) | mlproject |

### Passing cleanly on the host's native arch (12)

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
| [`minimal_template_example`](https://github.com/chap-models/minimal_template_example) | mlproject |
| [`rwanda_random_forest`](https://github.com/chap-models/rwanda_random_forest) | mlproject |
| [`Xiang_LSTM`](https://github.com/chap-models/Xiang_LSTM) | mlproject |
| [`Xiang_SVM`](https://github.com/chap-models/Xiang_SVM) | mlproject |

<!-- END-SNAPSHOT -->

The block above is auto-rendered from `last_report.json` by
`chap-models-checker render-status` (wired into `make run`). Hand-curated
context — the "why" behind each failing bucket, repos delisted from the
org discovery — lives in "Per-repo investigation notes" below.

---

## Migration to chapkit-images

The canonical replacement for legacy / private docker images is
[`chapkit-images`](https://github.com/dhis2-chap/chapkit-images) at
`ghcr.io/dhis2-chap/`:

| Variant                  | Use when                                                              |
| ------------------------ | --------------------------------------------------------------------- |
| `chapkit-py`             | Pure Python model. Use `-cli` if invoking the chapkit CLI inside.     |
| `chapkit-r`              | Base R only. Smallest R image (~385 MB).                              |
| `chapkit-r-tidyverse`    | R + `tidyverse`, `fable`, `tsibble`, `xgboost`, `randomForest`, etc.  |
| `chapkit-r-inla`         | R + tidyverse + INLA + spatial (`sf`, `spdep`, `dlnm`).               |

### Status of swap PRs

| Repo                  | PR                                                                                          | Status   | Replacement image                                       | Result                                |
| --------------------- | ------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------- | ------------------------------------- |
| `auto_ets`            | [#1](https://github.com/chap-models/auto_ets/pull/1)                                        | Merged   | `ghcr.io/dhis2-chap/chapkit-r-tidyverse:latest`         | Green: `docker_pull_failed` -> `pass` |
| `mean`                | [#1](https://github.com/chap-models/mean/pull/1)                                            | Merged   | `ghcr.io/dhis2-chap/chapkit-r-tidyverse:latest`         | Green: `docker_pull_failed` -> `pass` |
| `XGBoost_for_Malawi`  | [#2](https://github.com/chap-models/XGBoost_for_Malawi/pull/2)                              | Merged   | `ghcr.io/dhis2-chap/chapkit-r-tidyverse:latest` (added) | Green: `docker_image_missing_runtime` -> `pass` |
| `ewars_per_district`  | [#1](https://github.com/chap-models/ewars_per_district/pull/1)                              | Merged   | `ghcr.io/dhis2-chap/chapkit-r-inla:latest`              | Green: `docker_pull_failed` -> `pass` (image swap + `predict.R` `idx.pred` future-period filter, ~213s). |

### Other shipped fixes

| Repo                                        | PR                                                                                              | Status | Result                                                            |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------- | ------ | ----------------------------------------------------------------- |
| `Exponential_smoothing_state_space_model`   | [#1](https://github.com/chap-models/Exponential_smoothing_state_space_model/pull/1)             | Merged | YAML-only: `invalid_mlproject` -> `docker_image_missing_runtime` (image lacks `forecast`; train.R is also a stub) |
| `Xiang_LSTM`                                | [#2](https://github.com/chap-models/Xiang_LSTM/pull/2)                                          | Merged | Config + horizon defensive edits. Green: `model_runtime_error` -> `pass` (~22s). |
| `chapkit_minimalist_example_r` (was `minimalist_example_r_chapkit`) | [#1](https://github.com/chap-models/chapkit_minimalist_example_r/pull/1) | Merged | Renamed + re-scaffolded via `chapkit init --template shell-r`. Green: `schema_mismatch` -> `pass` (~11s). Follow-up [#2](https://github.com/chap-models/chapkit_minimalist_example_r/pull/2) refreshes README + adds GPL-3.0 LICENSE. |
| `chapkit_minimalist_example_py` (new repo) | -                                                                                              | Shipped | Net-new chapkit port of [`dhis2-chap/minimalist_example_uv`](https://github.com/dhis2-chap/minimalist_example_uv) via `chapkit init --template shell-py`. Same sklearn `LinearRegression` model. Green: pass (~36s). |
| `minimal_template_example`                  | [#2](https://github.com/chap-models/minimal_template_example/pull/2)                            | Open   | Same `KeyError: 'user_option_values'` as Xiang_LSTM (fixed via `.get()` defaults), plus drop dead RMSE return that read `disease_cases` from a target-less future CSV. |
| `rwanda_random_forest`                      | [#1](https://github.com/chap-models/rwanda_random_forest/pull/1)                                | Merged | Replace deprecated `sklearn` PyPI stub with `scikit-learn` in `pyenv.yaml`; cap `GroupKFold(n_splits=5)` to `min(5, n_locations)`. Green: `model_runtime_error` -> `pass`. |
| `Vietnam-dengue-superensemble`              | [#2](https://github.com/chap-models/Vietnam-dengue-superensemble/pull/2)                        | Merged | Set top-level `Feature.id` on every polygon in `input/historic_data.geojson` so chap-core's set_polygons join finds matches. Green: `nonzero_exit` -> `pass`. |

---

## Per-repo investigation notes

### `Exponential_smoothing_state_space_model` — partial fix shipped (delisted 2026-05-18)

> No longer discovered by the org sweep as of the 2026-05-18 run. Notes
> retained as historical record.


**Surface failure:** `invalid_mlproject` — pydantic rejected
`required_covariates: ` (bare key, parses as YAML null) with
`Input should be a valid list`.

**Surface fix:** [#1](https://github.com/chap-models/Exponential_smoothing_state_space_model/pull/1) -
one-line YAML edit, `required_covariates: []`.

**Underlying problem:** the model itself is unimplemented.
`train.R` contains:

```r
train_chap <- function(train_fn, model_fn){
  # should train the model here
}
```

…and additionally

- `if (length(args) == 2)` while the MLproject command passes 3 args
  (`{train_data} {model} {model_config}`) — the train block never
  fires.
- Inside that `if`, `train_chap(csv_fn, model_fn)` references the
  undefined `csv_fn` instead of the assigned `train_fn`.

So even after the YAML fix the model can't produce a saved model
file, and `predict.R` can't load one. After PR #1 merges, the
chap-models-checker bucket flips from `invalid_mlproject` to
`model_runtime_error` — progress, but not green. A working ETS
implementation needs to be authored before this repo can pass.

### `Vietnam-dengue-superensemble` — deferred

**Surface failure (in chap-models-checker sweep):** `nonzero_exit` with
`ModelConfigurationException: Was not able to format command Rscript
train.R {train_data} {model} {polygons}`.

**Why the surface error happens:** The MLproject's train command uses
chap-core's magic `{polygons}` placeholder (chap-core fills it in
when the dataset has a sibling `.geojson`). The model legitimately
needs polygons — `predict.R` does `st_read(geojson_fn)` to load
province geometries.

The sweep falls through to synthetic data because curated `laos_subset`
doesn't have 7 of the 8 required covariates (`minimum_temperature`,
`maximum_temperature`, `nino34_anomaly`,
`specific_surface_humidity`, `wind_speed`, `periurban_landcover`,
`urban_landcover`). The chap-models-checker synth generator only
writes a `.geojson` when `spec.requires_geo` is true — but chap-core's
`ModelTemplateConfigV2` pydantic schema rejects `requires_geo` as
`extra_forbidden`, so an MLproject can't declare it. The model
silently lacks the polygons signal, command formatting fails.

**Follow-up done:** chap-models-checker now infers `requires_geo`
from a `{polygons}` placeholder in any MLproject `entry_points`
command. The synth path now writes a sibling `.geojson` for
Vietnam-dengue-superensemble; the `ModelConfigurationException`
formatting failure is gone.

**Real root cause (found):** chap-core's `set_polygons` uses
**top-level `feature.id`** to filter dataset locations:

```python
# chap_core/spatio_temporal_data/temporal_dataclass.py:201
polygon_ids = {feature.id for feature in polygons.features}
self._data_dict = {loc: data for loc, data in ... if loc in polygon_ids}
```

`synthesize_geojson` was setting only `properties.id`, leaving
`feature.id` as `None`. Fixed by setting both `id=` and
`properties.id` to the same `location_<n>` string.

**Result after the fix:** chap-core now joins synthetic polygons to
synthetic dataset rows correctly. Vietnam-dengue-superensemble's
sweep duration jumps from 3.9s (immediate empty-dataset error) to
~14s (the model actually trains via INLA), and the failure now
surfaces as a genuine model issue: INLA's
`simplified.laplace`/`laplace` strategies both crash on the random
synth data. That's a model-on-noise numerical issue — out of scope
for the checker — and the synth-geo path is correct now for any
future geo-using model.

### `ewars_per_district` — resolved 2026-05-11

PR [#1](https://github.com/chap-models/ewars_per_district/pull/1) merged
2026-05-11T11:24Z. Two changes shipped together:

1. **Image swap.** `docker_env.image: docker_r_inla:latest`
   (unresolvable, no registry prefix) → `ghcr.io/dhis2-chap/chapkit-r-inla:latest`.
2. **`predict.R` idx.pred filter.** Original code did
   `idx.pred <- which(is.na(casestopred))`, which picked up any
   historic row whose `Cases` was `NA` alongside the intended future
   rows. The fix restricts to time periods that actually appear in the
   future dataset:

   ```r
   future_periods <- unique(future_df$time_period)
   idx.pred <- which(generated$data$time_period %in% future_periods &
                     is.na(casestopred))
   ```

The schema-check NaN failure that surfaced when only the image was
swapped (`pandera ... 'forecast' contains null values: 7021 rows of
NaN`) is gone. Sweep run at 2026-05-11T12:15Z passes in ~213s.

Image is amd64-only (R-INLA ships x86_64 binaries only), so the row
moves into the `--platform=linux/amd64` table — no path to remove that
caveat for this repo short of an INLA arm64 rebuild upstream.

---

## Roadmap

What's next, grouped by where the work has to happen. Targets relative
to the snapshot above.

### In flight — waiting on PR merge

Queue is empty. `minimal_template_example` PR
[#2](https://github.com/chap-models/minimal_template_example/pull/2)
merged; the repo passes cleanly on the host's native arch in the
2026-05-18 sweep.

### Tractable — fixable from our side

#### 1. Multi-arch image batch (15 repos)

15 repos pass only because the host pre-pulls their docker image with
`--platform=linux/amd64`. They'd fail on a pure arm64 deploy.

- Effort: 15 mostly-identical PRs (Dockerfile FROM tweak +
  `docker buildx build --platform linux/amd64,linux/arm64`, or a
  `chapkit-r-tidyverse` / `chapkit-r-inla` swap that already ships
  multi-arch). Some can't go multi-arch at all (R-INLA ships x86_64
  binaries only — `ewars_per_district`, `INLA_baseline_model`,
  `epidemiar_example_model`); those need a documented platform pin
  instead.
- Doesn't change the dashboard count (they're already `pass`), but
  removes the ⚠ caveat across the board.

### Model-author outreach — issues filed

These need real model work that we can't reasonably do without domain
knowledge. Issues filed against each repo with the run.log evidence and
a suggested resolution; resolution is on the model authors.

| Repo | Issue | State |
| --- | --- | --- |
| [`epidemiar_example_model`](https://github.com/chap-models/epidemiar_example_model) | [#1](https://github.com/chap-models/epidemiar_example_model/issues/1) — `dplyr::reframe()` / `seq.int()` crash on synthetic data | open |
| [`Exponential_smoothing_state_space_model`](https://github.com/chap-models/Exponential_smoothing_state_space_model) | [#2](https://github.com/chap-models/Exponential_smoothing_state_space_model/issues/2) — `train.R` empty stub framing | closed 2026-05-06 (scoped wrong — empty `train_chap` is canonical; the real rot needs a fresh issue) |
| [`Vietnam-dengue-superensemble`](https://github.com/chap-models/Vietnam-dengue-superensemble) | [#1](https://github.com/chap-models/Vietnam-dengue-superensemble/issues/1) — needs Vietnam covariates / robust missing-data handling | closed 2026-05-05 (addressed by upstream commit `e89ac66`, which introduced a different INLA-crash regression — see 2026-05-11 snapshot) |

### Checker improvements — this repo

Debt we've discussed but skipped, ordered by effort vs. value.

#### a. Auto-update the Snapshot section

Today the Snapshot section above is hand-edited; it'll drift the
moment a sweep runs without me remembering to update it. Add markdown
markers (`<!-- BEGIN-SNAPSHOT -->` / `<!-- END-SNAPSHOT -->`) and a
`chap-models-checker render-status` subcommand that rewrites the
section from `last_report.json`. Wire into `make run` so it stays in
sync automatically.

- Effort: ~1 day. New CLI subcommand + small splicer.
- Value: high. Eliminates the drift class of bug (e.g. the stale
  `minimalist_example_r_chapkit` ghost row that we caught manually).

#### b. Scheduled / cron sweep

`make run` is invoked manually. A nightly cron (or GitHub Actions
schedule + commit-back) would surface model regressions within a day.

- Effort: ~half a day. GitHub Actions workflow with a `schedule:`
  trigger + `git commit` + `git push` of the snapshot delta. Needs
  Docker-on-CI for the chapkit Docker path, which the existing
  `ci.yml` job pattern already proves works.
- Value: medium-high.
- Caveat: each sweep takes ~20 min; nightly is fine, hourly probably
  not.

#### c. Richer synth geojson properties

`synthesize_geojson` currently writes only `Feature.id` and
`properties.id`. We've already proven (Vietnam debugging) that this is
enough for chap-core's join. But if a future model reads other
properties (`name`, `parent`, `level`, …), it'll fail in a confusing
way. Pre-emptive: write a richer property bag matching the laos
shape.

- Effort: ~1 hour. Defer until a real model needs it.

### Suggested order

1. ~~File the 3 outreach issues~~ — done (links above).
2. ~~`ewars_per_district` predict.R patch~~ — done, PR [#1](https://github.com/chap-models/ewars_per_district/pull/1) merged 2026-05-11.
3. Multi-arch image batch (clears 15 ⚠ caveats, mostly mechanical).
4. Auto-update Snapshot section (eliminates a recurring drift).
5. Scheduled sweep workflow.

Item 3 is model-side work. Items 4-5 are this repo's debt. Item 4 is
the highest-value-per-hour entry now that the in-flight queue is
nearly drained; do that first if time is constrained.
