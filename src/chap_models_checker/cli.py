"""Sweep github.com/chap-models and report which models pass `chap eval`.

Discovers public repos, classifies each as MLproject- or chapkit-style,
prepares a matching dataset (curated when available, synthetic otherwise),
runs `chap eval` via `uvx --from chap-core`, and persists results to
`last_report.json` (the snapshot the hand-maintained STATUS.md is
summarised from).
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from chap_models_checker import chapkit_service, discover, report, runner, spec
from chap_models_checker.datagen import prepare_dataset
from chap_models_checker.models import (
    DataStrategy,
    FailureCategory,
    ModelSpec,
    ModelStyle,
    OnlyStyle,
    RepoInfo,
    Report,
    RunResult,
    RunStatus,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
log = logging.getLogger("chap_models_checker")

DEFAULT_SNAPSHOT_PATH = Path("last_report.json")


def _load_snapshot(path: Path) -> Report | None:
    if not path.exists():
        return None
    try:
        return Report.model_validate_json(path.read_text())
    except Exception:  # noqa: BLE001 — corrupted snapshots are treated as missing
        return None


def _merge_snapshots(existing: Report, fresh: Report) -> Report:
    """Merge ``fresh`` rows over ``existing`` rows, keyed by repo name.

    Used when ``run --repo X`` only re-tests a subset and we don't want to
    lose status for the rest. Fresh entries win on name conflict.

    Report-level ``started_at`` / ``finished_at`` reflect the combined
    span across all rows (oldest start, newest finish) rather than the
    fresh sweep alone — without that, a single ``--repo X`` rerun would
    make every untouched row appear freshly swept in ``list``. ``list``
    additionally honours each row's ``checked_at`` for per-row staleness.

    Legacy existing rows that predate the ``checked_at`` field get
    backfilled with ``existing.finished_at`` — otherwise a mixed
    snapshot (one fresh stamped row plus N unstamped legacy rows) would
    let ``render_list`` ignore the legacy rows when picking the oldest
    timestamp and show only the fresh one, defeating the staleness
    signal.
    """
    by_name: dict[str, RunResult] = {}
    for r in existing.repos:
        if r.checked_at is None:
            r = r.model_copy(update={"checked_at": existing.finished_at})
        by_name[r.repo.lower()] = r
    for r in fresh.repos:
        by_name[r.repo.lower()] = r
    merged_rows = sorted(by_name.values(), key=lambda r: r.repo.lower())

    return Report(
        chap_core_version=fresh.chap_core_version or existing.chap_core_version,
        repos=merged_rows,
        started_at=min(existing.started_at, fresh.started_at),
        finished_at=max(existing.finished_at, fresh.finished_at),
    )


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _process_repo(
    repo: RepoInfo,
    *,
    workdir: Path,
    example_data_dir: Path | None,
    chap_core_version: str | None,
    timeout: float,
    data_strategy: DataStrategy,
    only_style: OnlyStyle,
    spec_only: bool,
    console: Console,
) -> RunResult:
    repo_workdir = workdir / repo.name
    repo_workdir.mkdir(parents=True, exist_ok=True)
    log_path = repo_workdir / "run.log"

    console.print(f"[bold]→ {repo.name}[/]  ({repo.html_url})")

    # 1. classify — wrap in try/except so a transient raw.githubusercontent
    # blip on this one repo doesn't abort the whole sweep. spec.detect_style
    # uses httpx; treat any HTTP-side failure as a per-repo skip with the
    # spec_fetch_failed bucket so the rest of the org still runs.
    try:
        style = spec.detect_style(repo)
    except Exception as exc:  # noqa: BLE001
        return RunResult(
            repo=repo.name,
            style=ModelStyle.unknown,
            status=RunStatus.fail,
            failure=FailureCategory.spec_fetch_failed,
            error_excerpt=f"detect_style: {type(exc).__name__}: {exc}"[:1000],
        )
    if only_style != "all" and style.value != only_style:
        return RunResult(
            repo=repo.name,
            style=style,
            status=RunStatus.skip,
            note=f"only-style={only_style}",
        )
    if style == ModelStyle.unknown:
        return RunResult(
            repo=repo.name,
            style=style,
            status=RunStatus.skip,
            failure=FailureCategory.no_mlproject,
            note="no MLproject and no chapkit dependency",
            suggestion=runner.suggest_fix(FailureCategory.no_mlproject, ""),
        )

    if style == ModelStyle.mlproject:
        return _process_mlproject(
            repo,
            workdir=workdir,
            example_data_dir=example_data_dir,
            chap_core_version=chap_core_version,
            timeout=timeout,
            data_strategy=data_strategy,
            spec_only=spec_only,
            log_path=log_path,
            repo_workdir=repo_workdir,
        )
    return _process_chapkit(
        repo,
        workdir=workdir,
        example_data_dir=example_data_dir,
        chap_core_version=chap_core_version,
        timeout=timeout,
        data_strategy=data_strategy,
        spec_only=spec_only,
        log_path=log_path,
        repo_workdir=repo_workdir,
    )


def _prepare_data_or_fail(
    *,
    model_spec: ModelSpec,
    repo: RepoInfo,
    workdir: Path,
    example_data_dir: Path | None,
    data_strategy: DataStrategy,
    style: ModelStyle,
) -> tuple[Path, Path | None, str] | RunResult:
    """Either return ``(csv, geojson, source)`` or a ``RunResult`` describing the failure."""
    try:
        csv_path, geo_path, source = prepare_dataset(
            model_spec,
            workdir / "_data",
            strategy=data_strategy,
            example_data_dir=example_data_dir,
            repo=repo,
        )
    except Exception as exc:  # noqa: BLE001
        return RunResult(
            repo=repo.name,
            style=style,
            spec=model_spec,
            status=RunStatus.fail,
            failure=FailureCategory.other,
            note=f"dataset prep failed: {exc}",
        )
    return csv_path, geo_path, source


def _result_from_outcome(
    *,
    repo: RepoInfo,
    style: ModelStyle,
    model_spec: ModelSpec,
    outcome: runner.RunOutcome,
    csv_path: Path,
    output_file: Path,
    log_path: Path,
    note: str,
) -> RunResult:
    log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    # platform_override is rendered separately by report._cause_cell, so we
    # don't need to splice it into `note` here — doing both produced
    # `... · ⚠ platform=linux/amd64 · data=synthetic · platform=linux/amd64`
    # in the markdown table.
    platform_override = getattr(outcome, "platform_override", None)

    if outcome.returncode == 0 and outcome.output_exists:
        suggestion = None
        if platform_override:
            suggestion = (
                f"Ran only because the host pre-pulled the docker image with "
                f"`--platform={platform_override}`. The image lacks an arm64 manifest, so "
                "this would fail on an arm64 deploy. Author should publish a multi-arch "
                "image or the deploy needs to force the platform too."
            )
        return RunResult(
            repo=repo.name,
            style=style,
            spec=model_spec,
            status=RunStatus.pass_,
            duration_s=outcome.duration_s,
            log_path=str(log_path),
            dataset_path=str(csv_path),
            output_file=str(output_file),
            note=note,
            suggestion=suggestion,
            platform_override=platform_override,
            returncode=outcome.returncode,
            timed_out=outcome.timed_out,
        )
    failure = runner.classify_failure(outcome, log_text)
    return RunResult(
        repo=repo.name,
        style=style,
        spec=model_spec,
        status=RunStatus.fail,
        failure=failure,
        duration_s=outcome.duration_s,
        log_path=str(log_path),
        dataset_path=str(csv_path),
        error_excerpt=runner.excerpt_error(log_text),
        note=note,
        suggestion=runner.suggest_fix(failure, log_text),
        platform_override=platform_override,
        returncode=outcome.returncode,
        timed_out=outcome.timed_out,
    )


def _process_mlproject(
    repo: RepoInfo,
    *,
    workdir: Path,
    example_data_dir: Path | None,
    chap_core_version: str | None,
    timeout: float,
    data_strategy: DataStrategy,
    spec_only: bool,
    log_path: Path,
    repo_workdir: Path,
) -> RunResult:
    try:
        model_spec = spec.fetch_mlproject_spec(repo)
    except Exception as exc:  # noqa: BLE001
        return RunResult(
            repo=repo.name,
            style=ModelStyle.mlproject,
            status=RunStatus.fail,
            failure=FailureCategory.spec_fetch_failed,
            error_excerpt=str(exc)[:1000],
        )

    if spec_only:
        return RunResult(
            repo=repo.name,
            style=ModelStyle.mlproject,
            spec=model_spec,
            status=RunStatus.skip,
            failure=FailureCategory.spec_only,
            note="spec-only run",
        )

    data = _prepare_data_or_fail(
        model_spec=model_spec,
        repo=repo,
        workdir=workdir,
        example_data_dir=example_data_dir,
        data_strategy=data_strategy,
        style=ModelStyle.mlproject,
    )
    if isinstance(data, RunResult):
        return data
    csv_path, geo_path, source = data

    output_file = repo_workdir / "eval.nc"
    outcome = runner.run_chap_eval(
        chap_core_version=chap_core_version,
        model_path=repo.html_url,
        style=ModelStyle.mlproject,
        dataset_csv=csv_path,
        output_file=output_file,
        log_path=log_path,
        timeout=timeout,
        docker_image=model_spec.docker_image,
    )
    note = f"data={source}" + ("+geo" if geo_path else "")
    return _result_from_outcome(
        repo=repo,
        style=ModelStyle.mlproject,
        model_spec=model_spec,
        outcome=outcome,
        csv_path=csv_path,
        output_file=output_file,
        log_path=log_path,
        note=note,
    )


def _process_chapkit(
    repo: RepoInfo,
    *,
    workdir: Path,
    example_data_dir: Path | None,
    chap_core_version: str | None,
    timeout: float,
    data_strategy: DataStrategy,
    spec_only: bool,
    log_path: Path,
    repo_workdir: Path,
) -> RunResult:
    """Clone, boot the chapkit service, fetch spec, run chap eval against the live URL."""
    clone_dir = workdir / "_clones" / repo.name
    try:
        spec.clone_repo(repo, clone_dir)
    except subprocess.CalledProcessError as exc:
        return RunResult(
            repo=repo.name,
            style=ModelStyle.chapkit,
            status=RunStatus.fail,
            failure=FailureCategory.spec_fetch_failed,
            error_excerpt=f"git clone: {exc.stderr.decode(errors='replace')[:500] if exc.stderr else exc}",
        )

    try:
        with chapkit_service.chapkit_service(clone_dir, log_dir=repo_workdir) as url:
            try:
                info, schema = chapkit_service.fetch_info(url)
                model_spec = spec.chapkit_spec_from_info(repo.name, info, schema)
            except Exception as exc:  # noqa: BLE001
                return RunResult(
                    repo=repo.name,
                    style=ModelStyle.chapkit,
                    status=RunStatus.fail,
                    failure=FailureCategory.spec_fetch_failed,
                    error_excerpt=str(exc)[:1000],
                )

            if spec_only:
                return RunResult(
                    repo=repo.name,
                    style=ModelStyle.chapkit,
                    spec=model_spec,
                    status=RunStatus.skip,
                    failure=FailureCategory.spec_only,
                    note="spec-only run",
                )

            data = _prepare_data_or_fail(
                model_spec=model_spec,
                repo=repo,
                workdir=workdir,
                example_data_dir=example_data_dir,
                data_strategy=data_strategy,
                style=ModelStyle.chapkit,
            )
            if isinstance(data, RunResult):
                return data
            csv_path, geo_path, source = data

            output_file = repo_workdir / "eval.nc"
            outcome = runner.run_chap_eval(
                chap_core_version=chap_core_version,
                model_path=url,
                style=ModelStyle.chapkit,
                dataset_csv=csv_path,
                output_file=output_file,
                log_path=log_path,
                timeout=timeout,
            )
            note = f"data={source}" + ("+geo" if geo_path else "")
            return _result_from_outcome(
                repo=repo,
                style=ModelStyle.chapkit,
                model_spec=model_spec,
                outcome=outcome,
                csv_path=csv_path,
                output_file=output_file,
                log_path=log_path,
                note=note,
            )
    except chapkit_service.ChapkitServiceError as exc:
        return RunResult(
            repo=repo.name,
            style=ModelStyle.chapkit,
            status=RunStatus.fail,
            failure=FailureCategory.spec_fetch_failed,
            error_excerpt=str(exc)[:1000],
        )


# ---------------------------- commands -------------------------------------


@app.command("reclassify")
def reclassify_cmd(
    workdir: Annotated[
        Path,
        typer.Option(help="Workdir containing report.json from a previous `run`."),
    ] = Path("./work"),
    report_path: Annotated[
        Path | None,
        typer.Option(
            "--report",
            help="Path to report.json (defaults to <workdir>/report.json).",
        ),
    ] = None,
    snapshot: Annotated[
        Path | None,
        typer.Option(
            "--snapshot",
            help="Where to write a commit-friendly copy. Default: ./last_report.json.",
        ),
    ] = DEFAULT_SNAPSHOT_PATH,
) -> None:
    """Re-run failure classification on existing run logs, without re-running ``chap eval``.

    Useful after editing patterns in ``runner.py``. Rewrites ``report.md``
    and ``report.json`` in-place against the previous sweep's logs.
    """
    import json as _json

    console = Console()
    workdir = workdir.resolve()
    src = (report_path or (workdir / "report.json")).resolve()
    if not src.exists():
        console.print(f"[red]No report.json found at {src}.[/]")
        raise typer.Exit(code=2)

    with src.open() as f:
        data = _json.load(f)

    reclassified = 0
    for r in data.get("repos", []):
        if r.get("status") != RunStatus.fail.value:
            continue
        log_path_s = r.get("log_path")
        if not log_path_s:
            continue
        log_path = Path(log_path_s)
        if not log_path.exists():
            continue

        log_text = log_path.read_text(errors="replace")
        # Prefer the persisted returncode / timed_out from the snapshot so we
        # don't lose timeout / no_output_file buckets on rerun. Older
        # snapshots (pre-fix) lack these fields; fall back to log scan: the
        # runner always emits a `[chap-models-checker] TIMEOUT after Xs`
        # marker on timeout, which is the unambiguous signal.
        output_file_p = Path(r.get("output_file") or "/tmp/_reclassify_dummy.nc")
        output_exists = output_file_p.exists() and output_file_p.stat().st_size > 0
        timed_out = bool(r.get("timed_out")) or "[chap-models-checker] TIMEOUT after" in log_text
        rc = r.get("returncode")
        if rc is None:
            # No persisted returncode. Heuristic: a clean "Evaluation complete"
            # line + an output file means rc=0; otherwise treat as non-zero.
            rc = 0 if (output_exists and "Evaluation complete" in log_text and not timed_out) else 1
        outcome = runner.RunOutcome(
            cmd=[],
            returncode=int(rc),
            duration_s=float(r.get("duration_s") or 0.0),
            log_path=log_path,
            output_file=output_file_p,
            output_exists=output_exists,
            timed_out=timed_out,
        )
        new_failure = runner.classify_failure(outcome, log_text)
        r["failure"] = new_failure.value
        r["suggestion"] = runner.suggest_fix(new_failure, log_text)
        r["error_excerpt"] = runner.excerpt_error(log_text)
        # Strip stale ` · platform=...` suffixes from old notes — earlier
        # versions of _result_from_outcome appended that even though
        # report._cause_cell now renders platform_override on its own. Without
        # this, reclassified rows still display
        # `... · ⚠ platform=linux/amd64 · data=synthetic · platform=linux/amd64`.
        if r.get("note"):
            r["note"] = re.sub(r"\s*·\s*platform=\S+", "", r["note"]).strip(" ·")
        reclassified += 1

    full_report = Report.model_validate(data)
    report.render_console(full_report, console=console)
    md, js = report.write_outputs(full_report, workdir)
    console.print(f"\n[bold]Re-classified {reclassified} failure(s) from logs in {workdir}.[/]")
    console.print(f"[bold]Wrote[/] {md}\n[bold]Wrote[/] {js}")
    if snapshot is not None and str(snapshot) not in ("", "."):
        snap_path = report.write_snapshot(full_report, snapshot.resolve())
        console.print(f"[bold]Wrote[/] {snap_path}  [dim](commit-friendly snapshot)[/]")


@app.command("list")
def list_cmd(
    include_archived: Annotated[bool, typer.Option("--include-archived")] = False,
    snapshot: Annotated[
        Path,
        typer.Option(
            "--snapshot",
            help="Path to a checked-in report snapshot (defaults to ./last_report.json).",
        ),
    ] = DEFAULT_SNAPSHOT_PATH,
) -> None:
    """List public chap-models repos joined with their last-known pass/fail status.

    Reads ``last_report.json`` when present so it's easy to eyeball staleness
    versus needing a fresh sweep.
    """
    console = Console(width=160)
    repos = discover.list_chap_models_repos(include_archived=include_archived)
    snap = _load_snapshot(snapshot.resolve())
    report.render_list(repos, snap, console=console)
    if snap is None:
        console.print(f"[dim]No snapshot at {snapshot}. Run `chap-models-checker run` to populate it.[/]")


@app.command("run")
def run_cmd(
    repo: Annotated[
        list[str] | None,
        typer.Option("--repo", "-r", help="Filter to specific repo names (repeatable)."),
    ] = None,
    workdir: Annotated[
        Path,
        typer.Option(help="Where clones, datasets, logs, and reports are written."),
    ] = Path("./work"),
    example_data_dir: Annotated[
        Path | None,
        typer.Option(
            "--example-data-dir",
            help="Directory containing chap-core curated CSVs / geojsons.",
        ),
    ] = Path("../chap-core/example_data"),
    chap_core_version: Annotated[
        str | None,
        typer.Option(
            "--chap-core-version",
            help="Pin chap-core (passed as `uvx --from chap-core==VER`). Default: PyPI latest.",
        ),
    ] = None,
    timeout: Annotated[float, typer.Option(help="Per-repo chap eval timeout (seconds).")] = 900.0,
    data_strategy: Annotated[
        DataStrategy,
        typer.Option("--data-strategy", help="curated / synthetic / hybrid."),
    ] = "hybrid",
    only_style: Annotated[
        OnlyStyle,
        typer.Option("--only-style", help="Restrict to mlproject / chapkit / all."),
    ] = "all",
    include_archived: Annotated[bool, typer.Option("--include-archived")] = False,
    spec_only: Annotated[
        bool,
        typer.Option("--spec-only", help="Classify + extract specs, skip running chap eval."),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Process at most N repos (after filtering)."),
    ] = None,
    snapshot: Annotated[
        Path | None,
        typer.Option(
            "--snapshot",
            help="Where to write a commit-friendly copy of report.json. "
            "Default: ./last_report.json. Pass an empty string to disable.",
        ),
    ] = DEFAULT_SNAPSHOT_PATH,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run chap eval against every chap-models repo and report status."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s")
    console = Console()

    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    if example_data_dir is not None and not example_data_dir.exists():
        console.print(f"[yellow]example-data-dir {example_data_dir} not found — falling back to synthetic only.[/]")
        example_data_dir = None
    elif example_data_dir is not None:
        example_data_dir = example_data_dir.resolve()

    started_at = _now_iso()
    repos = discover.list_chap_models_repos(include_archived=include_archived)
    if repo:
        wanted = {r.lower() for r in repo}
        repos = [r for r in repos if r.name.lower() in wanted]
        missing = wanted - {r.name.lower() for r in repos}
        for m in sorted(missing):
            console.print(f"[yellow]repo not found in org: {m}[/]")
    if limit is not None:
        repos = repos[:limit]

    console.print(f"[bold]Discovered {len(repos)} repos[/]")
    if not repos:
        raise typer.Exit(code=1)

    results: list[RunResult] = []
    for r in repos:
        result = _process_repo(
            r,
            workdir=workdir,
            example_data_dir=example_data_dir,
            chap_core_version=chap_core_version,
            timeout=timeout,
            data_strategy=data_strategy,
            only_style=only_style,
            spec_only=spec_only,
            console=console,
        )
        # Stamp the per-repo timestamp here so every RunResult — regardless
        # of which internal branch produced it — carries an unambiguous
        # check time. Filtered runs (`--repo X`) merge into the existing
        # snapshot; without per-row stamps, the snapshot's report-level
        # finished_at would make all the un-rerun rows look freshly swept.
        if result.checked_at is None:
            result = result.model_copy(update={"checked_at": _now_iso()})
        results.append(result)
        status_color = {"pass": "green", "fail": "red", "skip": "yellow"}[result.status.value]
        cause = result.failure.value if result.failure else ""
        console.print(
            f"  [{status_color}]{result.status.value}[/]  {r.name}"
            f" ({result.style.value}, {result.duration_s:.1f}s) {cause}"
        )

    finished_at = _now_iso()
    full_report = Report(
        chap_core_version=chap_core_version,
        repos=results,
        started_at=started_at,
        finished_at=finished_at,
    )

    report.render_console(full_report, console=console)
    md, js = report.write_outputs(full_report, workdir)
    console.print(f"\n[bold]Wrote[/] {md}\n[bold]Wrote[/] {js}")
    if snapshot is not None and str(snapshot) not in ("", "."):
        snap_path = snapshot.resolve()
        # Merge with the existing snapshot when this was a filtered run, so
        # `--repo X` only updates X's row instead of nuking the rest.
        merged = full_report
        if repo:
            existing = _load_snapshot(snap_path)
            if existing is not None:
                merged = _merge_snapshots(existing, full_report)
        report.write_snapshot(merged, snap_path)
        console.print(f"[bold]Wrote[/] {snap_path}  [dim](commit-friendly snapshot)[/]")

    if full_report.failed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
