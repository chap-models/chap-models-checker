# chap-models triage notes

Hand-written companion to the auto-generated `findings.md`. Captures
investigation notes, root-cause analysis, and follow-up work that the
sweep itself can't see — durable across snapshot refreshes.

`findings.md` answers "what's failing right now". This file answers
"why, and what's the next move".

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
| `ewars_per_district`  | -                                                                                           | Deferred | n/a (see below)                                         | Image swap exposes a model bug        |

### Other shipped fixes

| Repo                                        | PR                                                                                              | Status | Result                                                            |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------- | ------ | ----------------------------------------------------------------- |
| `Exponential_smoothing_state_space_model`   | [#1](https://github.com/chap-models/Exponential_smoothing_state_space_model/pull/1)             | Merged | YAML-only: `invalid_mlproject` -> `docker_image_missing_runtime` (image lacks `forecast`; train.R is also a stub) |
| `Xiang_LSTM`                                | [#2](https://github.com/chap-models/Xiang_LSTM/pull/2)                                          | Open   | Config + horizon defensive edits; expected to flip `model_runtime_error` -> green |
| `chapkit_minimalist_example_r` (was `minimalist_example_r_chapkit`) | [#1](https://github.com/chap-models/chapkit_minimalist_example_r/pull/1) | Merged | Renamed + re-scaffolded via `chapkit init --template shell-r`. Green: `schema_mismatch` -> `pass` (~11s). Follow-up [#2](https://github.com/chap-models/chapkit_minimalist_example_r/pull/2) refreshes README + adds GPL-3.0 LICENSE. |
| `chapkit_minimalist_example_py` (new repo) | -                                                                                              | Shipped | Net-new chapkit port of [`dhis2-chap/minimalist_example_uv`](https://github.com/dhis2-chap/minimalist_example_uv) via `chapkit init --template shell-py`. Same sklearn `LinearRegression` model. Green: pass (~36s). |
| `minimal_template_example`                  | [#2](https://github.com/chap-models/minimal_template_example/pull/2)                            | Open   | Same `KeyError: 'user_option_values'` as Xiang_LSTM (fixed via `.get()` defaults), plus drop dead RMSE return that read `disease_cases` from a target-less future CSV. |
| `rwanda_random_forest`                      | [#1](https://github.com/chap-models/rwanda_random_forest/pull/1)                                | Open   | Replace deprecated `sklearn` PyPI stub with `scikit-learn` in `pyenv.yaml`; cap `GroupKFold(n_splits=5)` to `min(5, n_locations)` so the test dataset's 3 locations don't crash CV setup. |

## Per-repo notes

### `Exponential_smoothing_state_space_model` — partial fix shipped

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

**Why the surface fix isn't enough:** Tested locally against
`laos_subset.csv` (which has a sibling geojson, so chap-core auto-finds
it and `{polygons}` substitution works) — the model gets past the
formatting and into `predict.R`, where it crashes with
`Error in $<-.data.frame... replacement has 0 rows, data has 102`
while computing `dtr` (diurnal temperature range). The model relies
on Vietnam-specific covariates not present in any curated chap-core
dataset.

**Follow-up done:** chap-models-checker now infers `requires_geo`
from a `{polygons}` placeholder in any MLproject `entry_points`
command (commit `4bf512d`). The synth path now writes a sibling
`.geojson` for Vietnam-dengue-superensemble; the
`ModelConfigurationException` formatting failure is gone.

**Next layer that surfaced after the fix:** chap-core's dataset
loader joins the geojson into the `DataSet` and ends up with
`DataSet({})` (zero locations, zero rows). The model's MLproject
suggests reducing `--backtest-params.n-periods` or widening
`max_prediction_length`, but tested locally with
`max_prediction_length: 12` and `n_periods=1, n_splits=1`: same
empty-dataset error. So `max_prediction_length: 1` is a misleading
hint, not the actual cause.

The likely root cause is the synth geojson's properties being too
sparse. `synthesize_geojson` writes `{"id": "location_<n>"}` only;
`laos_subset.geojson` (which works) carries `code`, `name`, `level`,
`parent`, `parentGraph`, `groups`, `id`. chap-core's geojson→dataset
merge probably needs at least `name` or `parent` to produce a usable
hierarchy.

**Real root cause (found):** chap-core's
`set_polygons` uses **top-level `feature.id`** to filter dataset
locations:

```python
# chap_core/spatio_temporal_data/temporal_dataclass.py:201
polygon_ids = {feature.id for feature in polygons.features}
self._data_dict = {loc: data for loc, data in ... if loc in polygon_ids}
```

`synthesize_geojson` was setting only `properties.id`, leaving
`feature.id` as `None`. So `polygon_ids` was `{None}`, no CSV
location matched, every row got dropped — `DataSet({})`.

Property enrichment wasn't the issue at all; the missing field was
the top-level `Feature.id`. Fixed in `synthesize_geojson` by setting
both `id=` and `properties.id` to the same `location_<n>` string.

**Result after the fix:** chap-core now joins synthetic polygons to
synthetic dataset rows correctly. Vietnam-dengue-superensemble's
sweep duration jumps from 3.9s (immediate empty-dataset error) to
~14s (the model actually trains via INLA), and the failure now
surfaces as a genuine model issue: INLA's
`simplified.laplace`/`laplace` strategies both crash on the random
synth data. That's a model-on-noise numerical issue — out of scope
for the checker — and the synth-geo path is correct now for any
future geo-using model.

### `ewars_per_district` — deferred

**What was attempted:** Swap `docker_env.image: docker_r_inla:latest`
(unresolvable, no registry prefix) to
`ghcr.io/dhis2-chap/chapkit-r-inla:latest`.

**What happened:** The image swap works — chap-core pulls the new
image, runs `Rscript train.R` and `Rscript predict.R` for every
backtest split with no docker- or R-side error. The container exits
clean. But chap-core's pandera schema check then rejects the merged
predictions output:

```
pandera.errors.SchemaError: non-nullable series 'forecast' contains
null values: 7021 rows of NaN
```

**Cross-checked:** Re-ran the same eval against the legacy
`ghcr.io/dhis2-chap/docker_r_inla:master` image — produced the same
NaN failure. So the bug is in the model's predict logic, not in
either image. The legacy `docker_pull_failed` failure was masking it
because the model never actually ran.

**Likely root cause** in `predict.R`:

```r
casestopred <- generated$data$Cases     # response variable
idx.pred <- which(is.na(casestopred))   # rows to predict for
```

`generated$data` is the lag-extended union of historic + future. Any
historic row whose `Cases` is `NA` (gap-filled or genuinely missing in
the source data) gets included in `idx.pred` alongside the intended
future rows. The predictions CSV then carries rows for time periods
chap-core's join doesn't expect, leaving the actual forecast horizon
NaN after merge.

**Possible fix:** restrict `idx.pred` to `time_period` values that
appear in `future_df` only:

```r
future_periods <- unique(future_df$time_period)
idx.pred <- which(generated$data$time_period %in% future_periods &
                  is.na(casestopred))
```

(Untested — needs the model author or someone familiar with INLA-EWARS
output schemas to validate.)

**Net effect of the deferred swap:** would have moved this repo from
`docker_pull_failed` -> `model_runtime_error`. The plan's "progress,
not regression" still holds, but a green flip needs the predict.R
patch first. No PR opened until that work is done.

## Remaining failures

After the docker-image batch + the re-scaffold + checker improvements
landed, **seven failures remain** out of 28 repos (21 pass / 7 fail
in the 2026-05-04 20:24 -> 20:56 full sweep). Each needs the model
author or someone familiar with the model code to push a fix; they
aren't minimal-touch candidates from the checker side.

| Repo                                        | Failure              | Note                                                                                                                                |
| ------------------------------------------- | -------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `Exponential_smoothing_state_space_model`   | `docker_image_missing_runtime` | YAML fix ([#1](https://github.com/chap-models/Exponential_smoothing_state_space_model/pull/1)) shipped; the bucket flipped from `invalid_mlproject` but the model is still unimplemented (empty `train.R` stub, see per-repo notes above). |
| `epidemiar_example_model`                   | `nonzero_exit`       | R-side `seq.int()` / `dplyr::reframe()` error against synthetic data. Data-shape sensitive.                                          |
| `ewars_per_district`                        | `docker_pull_failed` | Image swap exposes a model-side NaN bug in `predict.R` (`idx.pred` includes historic NA rows). See per-repo notes above.            |
| `Vietnam-dengue-superensemble`              | `nonzero_exit`       | Synth-geo path now works (chap-models-checker commit `4bf512d` + `10b95e2`). Underlying failure is INLA-on-noise convergence; needs Vietnam-specific data or robust covariates. |
| `minimal_template_example`                  | `model_runtime_error`| Pure chap-core default base; train script bug. Not yet investigated.                                                                |
| `Xiang_LSTM`                                | `model_runtime_error`| PR [#2](https://github.com/chap-models/Xiang_LSTM/pull/2) open with config + horizon defensive edits. Awaiting merge.                |
| `rwanda_random_forest`                      | `model_runtime_error`| Pure chap-core default base; train script bug. Not yet investigated.                                                                |
