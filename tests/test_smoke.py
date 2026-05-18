"""Lightweight import + dispatch smoke tests.

Just enough so `make test` exits 0 instead of pytest's no-tests-collected
exit 5, and so a typo in a top-level module is caught at CI time without
needing a docker daemon or a network round-trip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chap_models_checker import chapkit_service, cli, datagen, discover, models, report, runner, spec  # noqa: F401
from chap_models_checker.models import (
    DataStrategy,
    FailureCategory,
    ModelSpec,
    ModelStyle,
    OnlyStyle,
    PeriodType,
    RepoInfo,
    Report,
    RunResult,
    RunStatus,
)


def test_modules_import() -> None:
    for mod in (chapkit_service, cli, datagen, discover, models, report, runner, spec):
        assert mod is not None


def test_cli_app_has_run_list_reclassify() -> None:
    commands = {cmd.name for cmd in cli.app.registered_commands}
    assert {"run", "list", "reclassify", "render-status"} <= commands


def _snapshot_report() -> Report:
    """Three-row report covering one fail, one amd64-pinned pass, one native pass."""
    return Report(
        chap_core_version=None,
        repos=[
            RunResult(
                repo="vietnam_dengue",
                style=ModelStyle.mlproject,
                status=RunStatus.fail,
                failure=FailureCategory.prediction_length,
                platform_override="linux/amd64",
                checked_at="2026-05-18T13:00:00+00:00",
            ),
            RunResult(
                repo="ewars_template",
                style=ModelStyle.mlproject,
                status=RunStatus.pass_,
                platform_override="linux/amd64",
                checked_at="2026-05-18T13:00:00+00:00",
            ),
            RunResult(
                repo="chtorch",
                style=ModelStyle.mlproject,
                status=RunStatus.pass_,
                platform_override=None,
                checked_at="2026-05-18T13:00:00+00:00",
            ),
        ],
        started_at="2026-05-18T12:30:00+00:00",
        finished_at="2026-05-18T13:10:00+00:00",
    )


def test_render_snapshot_block_buckets_repos_correctly() -> None:
    """One fail + one amd64-pinned pass + one native pass land in the right tables."""
    block = report.render_snapshot_block(_snapshot_report(), style="status")
    # Headers reflect the per-bucket counts.
    assert "### Failing (1)" in block
    assert "### Passing only with `--platform=linux/amd64` (1)" in block
    assert "### Passing cleanly on the host's native arch (1)" in block
    # Each repo appears in its expected section only.
    fail_section = block.split("### Failing")[1].split("### Passing only")[0]
    amd64_section = block.split("### Passing only")[1].split("### Passing cleanly")[0]
    native_section = block.split("### Passing cleanly")[1]
    assert "vietnam_dengue" in fail_section
    assert "ewars_template" in amd64_section
    assert "chtorch" in native_section
    # Bucket name is the failure enum value.
    assert "`prediction_length`" in fail_section


def test_render_snapshot_block_readme_adds_split_paragraph() -> None:
    """README variant exposes the native/amd64 split inline; STATUS doesn't."""
    readme = report.render_snapshot_block(_snapshot_report(), style="readme")
    status = report.render_snapshot_block(_snapshot_report(), style="status")
    assert "fully clean" in readme
    assert "fully clean" not in status
    # Both end with the close marker so the splicer can find it.
    assert readme.rstrip().endswith(report.MARKER_END)
    assert status.rstrip().endswith(report.MARKER_END)


def test_splice_marker_block_replaces_content_and_is_idempotent() -> None:
    """Splicing the same block twice is a no-op on the second pass."""
    original = (
        "# Header\n\nIntro prose.\n\n"
        f"{report.MARKER_BEGIN}\nOLD CONTENT\nMORE OLD\n{report.MARKER_END}\n\n"
        "Trailing prose.\n"
    )
    new_block = f"{report.MARKER_BEGIN}\nNEW CONTENT\n{report.MARKER_END}\n"

    first, changed1 = report.splice_marker_block(original, new_block=new_block)
    assert changed1 is True
    assert "OLD CONTENT" not in first
    assert "NEW CONTENT" in first
    # Surrounding prose preserved untouched.
    assert first.startswith("# Header\n\nIntro prose.\n\n")
    assert first.endswith("\nTrailing prose.\n")

    # Re-splicing the same block must report unchanged.
    second, changed2 = report.splice_marker_block(first, new_block=new_block)
    assert changed2 is False
    assert second == first


def test_splice_marker_block_raises_when_markers_missing() -> None:
    """Missing markers must be an error — we never silently append the block."""
    import pytest as _pytest

    new_block = f"{report.MARKER_BEGIN}\nx\n{report.MARKER_END}\n"
    with _pytest.raises(ValueError, match="snapshot markers not found"):
        report.splice_marker_block("no markers here\n", new_block=new_block)


def test_classify_failure_known_patterns() -> None:
    outcome = runner.RunOutcome(
        cmd=[],
        returncode=1,
        duration_s=0.0,
        log_path=Path("/tmp/_unused"),
        output_file=Path("/tmp/_unused.nc"),
        output_exists=False,
        timed_out=False,
    )
    assert runner.classify_failure(outcome, "No MLproject file found") == FailureCategory.no_mlproject
    assert (
        runner.classify_failure(outcome, "ImageNotFound: pull access denied for ets-r")
        == FailureCategory.docker_pull_failed
    )
    assert runner.classify_failure(outcome, "1 validation errors for MLServiceInfo") == FailureCategory.schema_mismatch


def test_classify_failure_timeout() -> None:
    outcome = runner.RunOutcome(
        cmd=[],
        returncode=-1,
        duration_s=900.0,
        log_path=Path("/tmp/_unused"),
        output_file=Path("/tmp/_unused.nc"),
        output_exists=False,
        timed_out=True,
    )
    assert runner.classify_failure(outcome, "anything") == FailureCategory.timeout


def test_classify_failure_no_output_file() -> None:
    outcome = runner.RunOutcome(
        cmd=[],
        returncode=0,
        duration_s=10.0,
        log_path=Path("/tmp/_unused"),
        output_file=Path("/tmp/_unused.nc"),
        output_exists=False,
        timed_out=False,
    )
    assert runner.classify_failure(outcome, "ran cleanly") == FailureCategory.no_output_file


def test_modelspec_round_trip() -> None:
    spec_in = ModelSpec(
        name="x",
        style=ModelStyle.mlproject,
        required_covariates=["population"],
        supported_period_type=PeriodType.month,
    )
    Report(
        chap_core_version=None,
        repos=[
            RunResult(
                repo="x",
                style=ModelStyle.mlproject,
                spec=spec_in,
                status=RunStatus.pass_,
            )
        ],
        started_at="2026-05-01T00:00:00+00:00",
        finished_at="2026-05-01T00:00:01+00:00",
    ).model_dump_json()


def test_find_curated_skips_geoless_when_geo_required(tmp_path: Path) -> None:
    """Regression: a weekly + requires_geo spec must NOT match nicaragua_weekly_subset
    (which ships no geojson) — that previously returned (csv, None, "curated")
    silently, running a geo-needing model without any geo input.
    """
    # Stub a fake curated dataset dir that mirrors the relevant chap-core layout.
    header = "time_period,location,disease_cases,population,rainfall,mean_temperature\n"
    (tmp_path / "laos_subset.csv").write_text(header)
    (tmp_path / "laos_subset.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    (tmp_path / "nicaragua_weekly_subset.csv").write_text(header)

    weekly_geo = ModelSpec(
        name="x",
        style=ModelStyle.mlproject,
        required_covariates=[],
        supported_period_type=PeriodType.week,
        requires_geo=True,
    )
    # Nicaragua matches period=week + covariates but has no geojson;
    # find_curated must skip it and return None.
    assert datagen.find_curated(weekly_geo, tmp_path) is None

    monthly_geo = weekly_geo.model_copy(update={"supported_period_type": PeriodType.month})
    csv_path, geo_path = datagen.find_curated(monthly_geo, tmp_path)  # type: ignore[misc]
    assert csv_path.name == "laos_subset.csv"
    assert geo_path is not None and geo_path.name == "laos_subset.geojson"


def test_clone_repo_refresh_success_does_not_reclone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Existing dest with a working `git fetch`+`reset` returns without re-cloning."""
    dest = tmp_path / "fake_repo"
    dest.mkdir()

    cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(spec.subprocess, "run", fake_run)
    repo = RepoInfo(
        name="r",
        full_name="o/r",
        default_branch="main",
        html_url="https://github.com/o/r",
        clone_url="git@github.com:o/r.git",
    )
    spec.clone_repo(repo, dest)

    assert any(c[:2] == ["git", "-C"] and "fetch" in c for c in cmds)
    assert any("reset" in c for c in cmds)
    assert not any("clone" in c for c in cmds)


def test_clone_repo_falls_back_to_full_clone_when_fetch_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Regression: a stale clone whose fetch breaks must trigger a re-clone, not silently use stale state."""
    dest = tmp_path / "fake_repo"
    dest.mkdir()

    cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        cmds.append(cmd)
        if "fetch" in cmd:
            raise subprocess.CalledProcessError(1, cmd, b"", b"network is hard")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(spec.subprocess, "run", fake_run)
    repo = RepoInfo(
        name="r",
        full_name="o/r",
        default_branch="main",
        html_url="https://github.com/o/r",
        clone_url="git@github.com:o/r.git",
    )
    spec.clone_repo(repo, dest)

    assert any("fetch" in c for c in cmds), "fetch should have been attempted"
    assert any("clone" in c for c in cmds), "fetch failure must trigger a re-clone"


def test_clone_repo_propagates_when_reclone_also_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If both refresh and re-clone fail, the caller sees CalledProcessError."""
    dest = tmp_path / "fake_repo"
    dest.mkdir()

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        if "fetch" in cmd or "clone" in cmd:
            raise subprocess.CalledProcessError(1, cmd, b"", b"boom")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(spec.subprocess, "run", fake_run)
    repo = RepoInfo(
        name="r",
        full_name="o/r",
        default_branch="main",
        html_url="https://github.com/o/r",
        clone_url="git@github.com:o/r.git",
    )
    with pytest.raises(subprocess.CalledProcessError):
        spec.clone_repo(repo, dest)


def test_render_markdown_escapes_pipes_and_newlines() -> None:
    """A `|` in a note / suggestion / covariate must not break the markdown table layout."""
    rep = Report(
        chap_core_version=None,
        repos=[
            RunResult(
                repo="weird-repo",
                style=ModelStyle.mlproject,
                spec=ModelSpec(
                    name="weird",
                    style=ModelStyle.mlproject,
                    required_covariates=["fancy|cov", "other"],
                    supported_period_type=PeriodType.month,
                ),
                status=RunStatus.fail,
                failure=FailureCategory.nonzero_exit,
                duration_s=1.0,
                note="data=synthetic | something",
                suggestion="run `chap | sort | head` and inspect",
            ),
        ],
        started_at="2026-05-01T00:00:00+00:00",
        finished_at="2026-05-01T00:00:01+00:00",
    )
    md = report.render_markdown(rep)
    body_row = next(line for line in md.splitlines() if line.startswith("| [`weird-repo`"))
    # 8 logical columns -> 9 pipe-bordered cells (leading + between + trailing).
    # Literal pipes inside cells must be backslash-escaped so the count is stable.
    assert body_row.count("|") - body_row.count(r"\|") == 9, body_row


def test_has_r_sources_finds_nested_files(tmp_path: Path) -> None:
    """Regression: nested R sources (src/<pkg>/model/foo.R) must be detected.

    The previous depth-1 glob only checked `./`, `./scripts`, and `./src`,
    so an R-backed chapkit repo with a deeper layout was misclassified as
    pure-Python and booted via `uv run` instead of docker.
    """
    nested = tmp_path / "src" / "mypkg" / "model"
    nested.mkdir(parents=True)
    (nested / "train.R").write_text("# nested R script\n")

    assert chapkit_service._has_r_sources(tmp_path) is True


def test_has_r_sources_skips_venvs_and_caches(tmp_path: Path) -> None:
    """An R file inside .venv / node_modules / __pycache__ shouldn't flip the runtime."""
    for noisy in (".venv", "node_modules", "__pycache__", ".git"):
        d = tmp_path / noisy / "deep"
        d.mkdir(parents=True)
        (d / "ignore.R").write_text("# vendored\n")

    assert chapkit_service._has_r_sources(tmp_path) is False


def test_ensure_dockerfile_omits_uv_lock_when_missing(tmp_path: Path) -> None:
    """Synthetic Dockerfile must not emit a wildcard COPY against an absent uv.lock.

    Some BuildKit frontends fail when `uv.lock*` matches nothing; we now
    only include the lock file when it's actually on disk.
    """
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    # Note: no uv.lock on purpose

    df = chapkit_service._ensure_dockerfile(tmp_path)
    text = df.read_text()
    assert "uv.lock" not in text, "synthetic Dockerfile must not reference an absent uv.lock"
    assert "COPY pyproject.toml ./" in text


def test_ensure_dockerfile_includes_uv_lock_when_present(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    (tmp_path / "uv.lock").write_text("# real lockfile\n")

    df = chapkit_service._ensure_dockerfile(tmp_path)
    text = df.read_text()
    assert "COPY pyproject.toml uv.lock ./" in text


def test_clone_repo_translates_missing_git_to_called_process_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing git CLI must surface as CalledProcessError so _process_chapkit catches it."""

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(spec.subprocess, "run", fake_run)
    repo = RepoInfo(
        name="r",
        full_name="o/r",
        default_branch="main",
        html_url="https://github.com/o/r",
        clone_url="git@github.com:o/r.git",
    )
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        spec.clone_repo(repo, tmp_path / "fresh")
    assert exc_info.value.returncode == 127
    assert b"git CLI not on PATH" in (exc_info.value.stderr or b"")


def test_clone_repo_translates_timeout_to_called_process_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A hung git operation must not stall the sweep — it surfaces as CalledProcessError."""

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1.0)

    monkeypatch.setattr(spec.subprocess, "run", fake_run)
    repo = RepoInfo(
        name="r",
        full_name="o/r",
        default_branch="main",
        html_url="https://github.com/o/r",
        clone_url="git@github.com:o/r.git",
    )
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        spec.clone_repo(repo, tmp_path / "fresh", clone_timeout=1.0)
    assert exc_info.value.returncode == 124
    assert b"timed out" in (exc_info.value.stderr or b"")


def test_coerce_str_list_rejects_string_value() -> None:
    """Regression: required_covariates: \"rainfall\" must NOT become per-character soup."""
    out = spec._coerce_str_list("rainfall", field_name="required_covariates", source="test")
    assert out == []  # not ["r","a","i","n","f","a","l","l"]


def test_coerce_str_list_passes_through_lists() -> None:
    out = spec._coerce_str_list(["a", "b"], field_name="required_covariates", source="test")
    assert out == ["a", "b"]


def test_coerce_str_list_handles_none() -> None:
    assert spec._coerce_str_list(None, field_name="required_covariates", source="test") == []


def test_entry_points_use_polygons_detects_placeholder() -> None:
    """Regression: an MLproject command referencing {polygons} must signal requires_geo."""
    data = {
        "entry_points": {
            "train": {"command": "Rscript train.R {train_data} {model} {polygons}"},
            "predict": {"command": "Rscript predict.R {model} {historic_data} {future_data} {out_file}"},
        }
    }
    assert spec._entry_points_use_polygons(data) is True


def test_entry_points_use_polygons_false_when_absent() -> None:
    data = {
        "entry_points": {
            "train": {"command": "python train.py {train_data} {model}"},
        }
    }
    assert spec._entry_points_use_polygons(data) is False


def test_find_repo_bundled_data_returns_csv_and_geojson_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: prepare_dataset must prefer the model's own input/training_data.csv."""

    class _FakeResp:
        def __init__(self, status_code: int, content: bytes = b"") -> None:
            self.status_code = status_code
            self.content = content

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"http {self.status_code}")

    class _FakeClient:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get(self, url: str, *_: object, **__: object) -> _FakeResp:
            if url.endswith("/input/training_data.csv"):
                return _FakeResp(200, b"time_period,location\n2020-01,a\n")
            if url.endswith("/input/training_data.geojson"):
                return _FakeResp(200, b'{"type":"FeatureCollection","features":[]}')
            return _FakeResp(404)

    monkeypatch.setattr(datagen.httpx, "Client", _FakeClient)
    repo = RepoInfo(
        name="r",
        full_name="o/r",
        default_branch="main",
        html_url="https://github.com/o/r",
        clone_url="git@github.com:o/r.git",
    )
    out = datagen.find_repo_bundled_data(repo, tmp_path / "cache")
    assert out is not None
    csv_path, geo_path = out
    assert csv_path.name == "training_data.csv"
    assert csv_path.read_bytes().startswith(b"time_period,location")
    assert geo_path is not None
    assert geo_path.read_bytes().startswith(b'{"type":"FeatureCollection"')


def test_find_repo_bundled_data_returns_none_when_nothing_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: when no candidate path is reachable, return None so the caller falls through."""

    class _FakeResp:
        status_code = 404

        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get(self, *_: object, **__: object) -> _FakeResp:
            return _FakeResp()

    monkeypatch.setattr(datagen.httpx, "Client", _FakeClient)
    repo = RepoInfo(
        name="r",
        full_name="o/r",
        default_branch="main",
        html_url="https://github.com/o/r",
        clone_url="git@github.com:o/r.git",
    )
    assert datagen.find_repo_bundled_data(repo, tmp_path / "cache") is None


def test_synthesize_geojson_sets_top_level_feature_id(tmp_path: Path) -> None:
    """Regression: chap-core joins on feature.id; if it's missing, DataSet({}) results.

    chap_core.spatio_temporal_data.temporal_dataclass.set_polygons builds
    `polygon_ids = {feature.id for feature in features}` and drops any
    location whose name isn't in that set. We previously only set
    `properties.id`, leaving `feature.id = None`, so every dataset row
    got filtered out.
    """
    import json

    from chap_models_checker.datagen import synthesize_geojson

    spec = ModelSpec(
        name="x",
        style=ModelStyle.mlproject,
        required_covariates=[],
        supported_period_type=PeriodType.month,
        requires_geo=True,
    )
    out = synthesize_geojson(spec, tmp_path / "data.geojson", num_locations=3)
    payload = json.loads(out.read_text())
    ids = [feature["id"] for feature in payload["features"]]
    assert ids == ["location_0", "location_1", "location_2"]
    # And properties.id stays in lockstep so downstream code that prefers it works too.
    assert [feature["properties"]["id"] for feature in payload["features"]] == ids


def test_entry_points_use_polygons_handles_missing_or_malformed() -> None:
    assert spec._entry_points_use_polygons({}) is False
    assert spec._entry_points_use_polygons({"entry_points": None}) is False
    assert spec._entry_points_use_polygons({"entry_points": "not a dict"}) is False
    assert spec._entry_points_use_polygons({"entry_points": {"train": "not a dict"}}) is False
    assert spec._entry_points_use_polygons({"entry_points": {"train": {"command": None}}}) is False


def test_merge_snapshots_preserves_existing_row_checked_at() -> None:
    """Regression: filtered run must NOT make untouched rows look freshly swept."""
    old_ts = "2026-04-01T00:00:00+00:00"
    new_ts = "2026-05-01T00:00:00+00:00"

    existing = Report(
        chap_core_version=None,
        started_at=old_ts,
        finished_at=old_ts,
        repos=[
            RunResult(repo="a", style=ModelStyle.mlproject, status=RunStatus.pass_, checked_at=old_ts),
            RunResult(repo="b", style=ModelStyle.mlproject, status=RunStatus.fail, checked_at=old_ts),
            RunResult(repo="c", style=ModelStyle.mlproject, status=RunStatus.pass_, checked_at=old_ts),
        ],
    )
    fresh = Report(
        chap_core_version=None,
        started_at=new_ts,
        finished_at=new_ts,
        repos=[
            # Only `b` was re-tested.
            RunResult(repo="b", style=ModelStyle.mlproject, status=RunStatus.pass_, checked_at=new_ts),
        ],
    )

    merged = cli._merge_snapshots(existing, fresh)
    by_name = {r.repo: r for r in merged.repos}
    # Untouched rows keep their OLD checked_at; only `b` gets the fresh one.
    assert by_name["a"].checked_at == old_ts
    assert by_name["c"].checked_at == old_ts
    assert by_name["b"].checked_at == new_ts
    # Report-level span covers both — oldest start, newest finish.
    assert merged.started_at == old_ts
    assert merged.finished_at == new_ts


def test_merge_backfills_legacy_unstamped_rows(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: legacy snapshot (no checked_at on existing rows) must not get
    "freshened" by a single filtered rerun. Existing rows backfill from
    existing.finished_at during merge.
    """
    old_ts = "2026-04-01T00:00:00+00:00"
    new_ts = "2026-05-01T00:00:00+00:00"

    existing = Report(
        chap_core_version=None,
        started_at=old_ts,
        finished_at=old_ts,
        repos=[
            # Note: NO checked_at on existing rows (simulates pre-feature snapshot).
            RunResult(repo="a", style=ModelStyle.mlproject, status=RunStatus.pass_),
            RunResult(repo="b", style=ModelStyle.mlproject, status=RunStatus.fail),
        ],
    )
    fresh = Report(
        chap_core_version=None,
        started_at=new_ts,
        finished_at=new_ts,
        repos=[
            # Only `b` re-tested, with the new field populated.
            RunResult(repo="b", style=ModelStyle.mlproject, status=RunStatus.pass_, checked_at=new_ts),
        ],
    )
    merged = cli._merge_snapshots(existing, fresh)
    by_name = {r.repo: r for r in merged.repos}
    # Untouched legacy row gets backfilled to existing.finished_at.
    assert by_name["a"].checked_at == old_ts
    # Re-tested row keeps the fresh timestamp.
    assert by_name["b"].checked_at == new_ts


def test_render_list_falls_back_to_finished_at_when_rows_unstamped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Loading a pre-feature snapshot directly (no merge) must still surface real age."""
    old_ts = "2026-04-01T00:00:00+00:00"
    rep = Report(
        chap_core_version=None,
        started_at=old_ts,
        finished_at=old_ts,
        repos=[
            RunResult(repo="a", style=ModelStyle.mlproject, status=RunStatus.pass_),  # unstamped
        ],
    )
    discovered = [
        RepoInfo(
            name="a",
            full_name="o/a",
            default_branch="main",
            html_url="https://github.com/o/a",
            clone_url="git@github.com:o/a.git",
        )
    ]
    from rich.console import Console as _Console

    console = _Console(width=200, force_terminal=False, no_color=True)
    report.render_list(discovered, rep, console=console)
    out = capsys.readouterr().out
    assert old_ts in out


def test_render_list_uses_oldest_row_for_staleness(capsys: pytest.CaptureFixture[str]) -> None:
    """Title should reflect the oldest row's checked_at, not the most recent merge."""
    old_ts = "2026-04-01T00:00:00+00:00"
    new_ts = "2026-05-01T00:00:00+00:00"

    rep = Report(
        chap_core_version=None,
        started_at=old_ts,
        finished_at=new_ts,  # would falsely look fresh under the old logic
        repos=[
            RunResult(repo="a", style=ModelStyle.mlproject, status=RunStatus.pass_, checked_at=old_ts),
            RunResult(repo="b", style=ModelStyle.mlproject, status=RunStatus.pass_, checked_at=new_ts),
        ],
    )
    discovered = [
        RepoInfo(
            name=name,
            full_name=f"o/{name}",
            default_branch="main",
            html_url=f"https://github.com/o/{name}",
            clone_url=f"git@github.com:o/{name}.git",
        )
        for name in ("a", "b")
    ]

    from rich.console import Console as _Console

    console = _Console(width=200, force_terminal=False, no_color=True)
    report.render_list(discovered, rep, console=console)
    out = capsys.readouterr().out
    # Title shows the OLDEST row's timestamp, not finished_at.
    assert old_ts in out
    assert "oldest row" in out


def test_list_chap_models_repos_skips_self(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: chap-models-checker (the tool) must not be discovered as a model.

    Once the checker is pushed to chap-models/ alongside the actual models, the
    GitHub org listing returns it. Without the SKIPPED_REPOS filter, the sweep
    would try to clone + run chap eval against the tooling repo itself.
    """
    fake_response_repos = [
        {
            "name": "chap-models-checker",
            "full_name": "chap-models/chap-models-checker",
            "default_branch": "main",
            "archived": False,
            "description": "the tooling itself",
            "html_url": "https://github.com/chap-models/chap-models-checker",
            "clone_url": "git@github.com:chap-models/chap-models-checker.git",
        },
        {
            "name": "auto_arima",
            "full_name": "chap-models/auto_arima",
            "default_branch": "main",
            "archived": False,
            "description": "a real model",
            "html_url": "https://github.com/chap-models/auto_arima",
            "clone_url": "git@github.com:chap-models/auto_arima.git",
        },
    ]

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, object]]:
            # Single page; second call returns []
            nonlocal_state["calls"] += 1
            return fake_response_repos if nonlocal_state["calls"] == 1 else []

    nonlocal_state = {"calls": 0}

    class _FakeClient:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get(self, *_: object, **__: object) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(discover.httpx, "Client", _FakeClient)

    repos = discover.list_chap_models_repos()
    names = [r.name for r in repos]
    assert names == ["auto_arima"], names


def test_typing_choices_exposed() -> None:
    # DataStrategy / OnlyStyle / RepoInfo are part of the public surface used by callers
    assert "curated" in DataStrategy.__args__  # type: ignore[attr-defined]
    assert "all" in OnlyStyle.__args__  # type: ignore[attr-defined]
    repo = RepoInfo(
        name="r",
        full_name="org/r",
        default_branch="main",
        html_url="https://github.com/org/r",
        clone_url="git@github.com:org/r.git",
    )
    assert repo.raw_url_prefix.endswith("/main")
