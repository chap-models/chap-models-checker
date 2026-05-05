"""Pick a curated dataset, or synthesize one matching the model spec.

Curated lookup is consulted first when ``strategy="hybrid"``; if no curated
file matches the (period, requires_geo, covariate-set) combination we fall
through to synthetic generation. The synthetic generator is a thin
re-implementation of ``chapkit.cli.test.generator.TestDataGenerator`` that
emits the canonical column set (``time_period, location, disease_cases,
population, rainfall, mean_temperature``) plus any extra required /
additional / free covariates the spec declares.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Literal

import httpx
from geojson_pydantic import Feature, FeatureCollection, Point, Polygon
from geojson_pydantic.types import Position2D
from pydantic import BaseModel, ConfigDict

from chap_models_checker.models import DataStrategy, ModelSpec, PeriodType, RepoInfo

# ---------------------------- curated lookup --------------------------------


class CuratedDataset(BaseModel):
    """A bundled CSV (with optional sibling geojson) we know matches a covariate set."""

    model_config = ConfigDict(frozen=True)

    csv: str
    geojson: str | None
    period: PeriodType
    columns: frozenset[str]


# Order matters: prefer richer columns (more covariates available) first.
CURATED: list[CuratedDataset] = [
    CuratedDataset(
        csv="laos_subset.csv",
        geojson="laos_subset.geojson",
        period=PeriodType.month,
        columns=frozenset({"rainfall", "mean_temperature", "population"}),
    ),
    CuratedDataset(
        csv="nicaragua_weekly_subset.csv",
        geojson=None,
        period=PeriodType.week,
        columns=frozenset({"rainfall", "mean_temperature", "population"}),
    ),
]


def _matches_curated(spec: ModelSpec, ds: CuratedDataset) -> bool:
    if spec.supported_period_type not in (PeriodType.any, ds.period):
        return False
    # Both required and (resolved) additional continuous covariates must be present
    # in the curated CSV. Falling through to synth is fine when they're not.
    needed = set(spec.required_covariates) | set(spec.additional_continuous_covariates)
    return needed.issubset(ds.columns)


def find_curated(spec: ModelSpec, example_data_dir: Path) -> tuple[Path, Path | None] | None:
    """Return ``(csv, geojson|None)`` paths if a curated dataset matches, else ``None``.

    Honors ``spec.requires_geo``: a curated dataset that doesn't ship a
    geojson (or whose geojson is missing on disk) is skipped when the spec
    requires geo, falling through to either the next curated entry or to
    synthetic generation instead of silently running geo-needing models
    without geo.
    """
    for ds in CURATED:
        if not _matches_curated(spec, ds):
            continue
        csv_path = example_data_dir / ds.csv
        if not csv_path.exists():
            continue
        geo_path: Path | None = None
        if ds.geojson is not None:
            candidate = example_data_dir / ds.geojson
            if candidate.exists():
                geo_path = candidate
        if spec.requires_geo and geo_path is None:
            # curated dataset doesn't satisfy the geo requirement; skip it
            # so synth fallback kicks in.
            continue
        return csv_path, geo_path
    return None


# ---------------------------- synthetic ------------------------------------


def _period_label(period: PeriodType, idx: int, start_year: int = 2020) -> str:
    if period == PeriodType.week:
        year = start_year + (idx // 52)
        week = (idx % 52) + 1
        return f"{year}-W{week:02d}"
    # month / any / year all use YYYY-MM (chap-core's any-resolution input)
    year = start_year + (idx // 12)
    month = (idx % 12) + 1
    return f"{year}-{month:02d}"


def synthesize_csv(
    spec: ModelSpec,
    out_csv: Path,
    *,
    num_locations: int = 5,
    num_periods: int = 60,
    seed: int = 42,
    start_year: int = 2020,
) -> Path:
    """Write a panel CSV that satisfies the model spec.

    Always emits the canonical columns (``time_period, location, disease_cases,
    population, rainfall, mean_temperature``) plus any required / additional /
    free-extra covariates declared in the spec. Extra free covariates are
    emitted only if the model allows them.
    """
    rnd = random.Random(seed)

    canonical = ["population", "rainfall", "mean_temperature"]
    required = [c for c in spec.required_covariates if c not in canonical and c != spec.target]
    additional = [c for c in spec.additional_continuous_covariates if c not in canonical and c not in required]
    extras: list[str] = []
    if spec.allow_free_additional_continuous_covariates:
        extras = ["extra_covariate_0", "extra_covariate_1"]

    columns = ["time_period", "location", spec.target, *canonical, *required, *additional, *extras]
    period = spec.supported_period_type
    if period == PeriodType.any:
        period = PeriodType.month  # canonical default

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for p in range(num_periods):
            for loc in range(num_locations):
                row: list[object] = [_period_label(period, p, start_year)]
                row.append(f"location_{loc}")
                row.append(float(rnd.randint(1, 100)))  # target
                row.append(rnd.randint(50_000, 1_500_000))  # population
                row.append(round(rnd.uniform(0, 400), 3))  # rainfall
                row.append(round(rnd.uniform(10, 35), 3))  # mean_temperature
                for _ in required:
                    row.append(round(rnd.uniform(0, 100), 3))
                for _ in additional:
                    row.append(round(rnd.uniform(0, 100), 3))
                for _ in extras:
                    row.append(round(rnd.uniform(0, 100), 3))
                writer.writerow(row)
    return out_csv


def synthesize_geojson(
    spec: ModelSpec,
    out_path: Path,
    *,
    num_locations: int = 5,
    geo_type: Literal["polygon", "point"] = "polygon",
    seed: int = 42,
) -> Path:
    """Write a GeoJSON FeatureCollection with one feature per synthetic location.

    Builds the FeatureCollection through ``geojson_pydantic`` so geometry
    types, coordinate ranges, and polygon ring closure are validated at
    synthesis time rather than failing later inside chap eval.
    """
    rnd = random.Random(seed)
    features: list[Feature[Point | Polygon, dict[str, str]]] = []
    for i in range(num_locations):
        lon = rnd.uniform(-170, 170)
        lat = rnd.uniform(-80, 80)
        geometry: Point | Polygon
        if geo_type == "point":
            geometry = Point(type="Point", coordinates=Position2D(lon, lat))
        else:
            size = 0.5
            geometry = Polygon(
                type="Polygon",
                coordinates=[
                    [
                        Position2D(lon - size, lat - size),
                        Position2D(lon + size, lat - size),
                        Position2D(lon + size, lat + size),
                        Position2D(lon - size, lat + size),
                        Position2D(lon - size, lat - size),
                    ]
                ],
            )
        location_id = f"location_{i}"
        # Top-level Feature.id (RFC 7946 §3.2) is what chap-core's
        # `set_polygons` joins against the dataset's location names
        # (`polygon_ids = {feature.id for feature in features}` and any
        # location not in that set gets dropped, yielding DataSet({}) when
        # we leave id unset). properties.id alone isn't enough.
        features.append(
            Feature(type="Feature", id=location_id, geometry=geometry, properties={"id": location_id}),
        )
    fc: FeatureCollection[Feature[Point | Polygon, dict[str, str]]] = FeatureCollection(
        type="FeatureCollection",
        features=features,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(fc.model_dump_json(indent=2, exclude_none=True))
    return out_path


# ---------------------------- bundled-in-repo lookup ------------------------

# Filenames to probe under the model's `input/` dir, in priority order.
# `training_data.csv` is the most canonical name; the others cover real
# patterns we've seen in the chap-models org (epidemiar's `laos_test_data.csv`,
# the bare `data.csv` some templates use, and a reserved
# `chap_eval_data.csv` for repos that prefer an explicit name).
_BUNDLED_CSV_CANDIDATES: tuple[str, ...] = (
    "input/training_data.csv",
    "input/laos_test_data.csv",
    "input/data.csv",
    "input/chap_eval_data.csv",
)


def find_repo_bundled_data(
    repo: RepoInfo,
    cache_dir: Path,
    *,
    timeout: float = 30.0,
) -> tuple[Path, Path | None] | None:
    """Look for example data the model author bundled at ``input/`` in their repo.

    Returns ``(csv, geojson|None)`` cached under ``cache_dir``, or ``None``
    when no candidate file is reachable. Probes the names in
    ``_BUNDLED_CSV_CANDIDATES`` in order via raw.githubusercontent (no
    clone). When a CSV is found, looks for a sibling ``.geojson`` with
    the same basename and downloads it if present.

    Preferred over chap-core's curated lookup because the model author
    knows best which data their model was tuned for. The synth fallback
    still kicks in when nothing is bundled.
    """
    base = repo.raw_url_prefix
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for path in _BUNDLED_CSV_CANDIDATES:
            url = f"{base}/{path}"
            csv_resp = client.get(url)
            if csv_resp.status_code == 404:
                continue
            if csv_resp.status_code != 200:
                csv_resp.raise_for_status()
            cache_dir.mkdir(parents=True, exist_ok=True)
            csv_path = cache_dir / Path(path).name
            csv_path.write_bytes(csv_resp.content)

            geo_url = url[: -len(".csv")] + ".geojson"
            geo_resp = client.get(geo_url)
            geo_path: Path | None = None
            if geo_resp.status_code == 200:
                geo_path = csv_path.with_suffix(".geojson")
                geo_path.write_bytes(geo_resp.content)
            return csv_path, geo_path
    return None


# ---------------------------- top-level entry ------------------------------


def prepare_dataset(
    spec: ModelSpec,
    workdir: Path,
    *,
    strategy: DataStrategy,
    example_data_dir: Path | None,
    repo: RepoInfo,
) -> tuple[Path, Path | None, str]:
    """Return ``(csv, geojson|None, source)``.

    ``source`` is one of ``"bundled"`` (model's own ``input/``),
    ``"curated"`` (chap-core's example_data), or ``"synthetic"``.

    Lookup order under ``hybrid`` (the default): bundled > curated > synth.
    Under ``curated``: bundled > curated, and a hard error if neither
    matches (no synth fallback). Under ``synthetic``: synth always.

    Invariant: when ``geojson`` is non-``None``, it lives next to the CSV
    with the same basename + ``.geojson`` suffix. chap eval has no
    explicit ``--geojson`` flag — it auto-discovers the sibling geojson.
    We enforce co-location here so the runner's ``+geo`` note isn't a lie.
    """
    if strategy in ("curated", "hybrid"):
        bundled = find_repo_bundled_data(repo, workdir / "_bundled" / repo.name)
        if bundled is not None:
            csv_path, geo_path = bundled
            return csv_path, _ensure_colocated_geojson(csv_path, geo_path), "bundled"

        if example_data_dir is not None:
            match = find_curated(spec, example_data_dir)
            if match is not None:
                csv_path, geo_path = match
                return csv_path, _ensure_colocated_geojson(csv_path, geo_path), "curated"

        if strategy == "curated":
            raise RuntimeError(
                f"No bundled or curated dataset for {repo.name} "
                f"(period={spec.supported_period_type.value}, covariates={spec.required_covariates})"
            )

    repo_dir = workdir / repo.name
    repo_dir.mkdir(parents=True, exist_ok=True)
    csv_path = synthesize_csv(spec, repo_dir / "data.csv")
    synth_geo: Path | None = None
    if spec.requires_geo:
        synth_geo = synthesize_geojson(spec, repo_dir / "data.geojson")
    return csv_path, _ensure_colocated_geojson(csv_path, synth_geo), "synthetic"


def _ensure_colocated_geojson(csv_path: Path, geo_path: Path | None) -> Path | None:
    """Verify or copy the geojson so chap eval's auto-discover finds it.

    chap eval looks for ``<csv-basename>.geojson`` next to the CSV. If
    ``geo_path`` is already at that location we pass it through; if it's
    elsewhere on disk we copy it into place (curated example data sometimes
    lives outside the repo). Returns ``None`` if no geojson was supplied.
    """
    if geo_path is None:
        return None
    expected = csv_path.with_suffix(".geojson")
    if geo_path.resolve() == expected.resolve():
        return expected
    import shutil

    shutil.copy2(geo_path, expected)
    return expected
