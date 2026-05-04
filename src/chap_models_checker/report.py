"""Render check results as a Rich table, Markdown, and JSON."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chap_models_checker.models import ModelStyle, RepoInfo, Report, RunResult, RunStatus

__all__ = [
    "ModelStyle",
    "RepoInfo",
    "render_console",
    "render_list",
    "render_markdown",
    "write_outputs",
    "write_snapshot",
]

_STATUS_STYLE = {
    RunStatus.pass_: "green",
    RunStatus.fail: "red",
    RunStatus.skip: "yellow",
}


def _covariates_cell(result: RunResult) -> str:
    if result.spec is None:
        return "—"
    parts: list[str] = []
    if result.spec.required_covariates:
        parts.append("req: " + ", ".join(result.spec.required_covariates))
    if result.spec.additional_continuous_covariates:
        parts.append("acc: " + ", ".join(result.spec.additional_continuous_covariates))
    if result.spec.allow_free_additional_continuous_covariates:
        parts.append("free")
    if result.spec.requires_geo:
        parts.append("geo")
    return "\n".join(parts) or "—"


def _period_cell(result: RunResult) -> str:
    return result.spec.supported_period_type.value if result.spec else "—"


def _cause_cell(result: RunResult) -> str:
    bits: list[str] = []
    if result.status == RunStatus.pass_:
        # Pass rows usually stay empty, but surface platform overrides so the
        # reader knows the green tick came with an asterisk.
        if result.platform_override:
            bits.append(f"⚠ platform={result.platform_override}")
        return " · ".join(bits)
    if result.failure is not None:
        bits.append(result.failure.value)
    if result.platform_override:
        bits.append(f"⚠ platform={result.platform_override}")
    if result.note:
        bits.append(result.note)
    return " · ".join(bits)


def _suggestion_cell(result: RunResult) -> str:
    return result.suggestion or ""


_STATUS_ICON = {
    RunStatus.pass_: "✓",
    RunStatus.fail: "✗",
    RunStatus.skip: "–",
}

# Failures first, then skips, then passes — most actionable on top.
_STATUS_ORDER = {RunStatus.fail: 0, RunStatus.skip: 1, RunStatus.pass_: 2}


def _truncate_excerpt(text: str | None, *, max_lines: int = 3, max_chars: int = 280) -> str:
    """Trim an error excerpt down to the first few lines / chars for the panel body."""
    if not text:
        return ""
    lines = text.strip().splitlines()
    head = lines[:max_lines]
    out = "\n".join(head)
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "…"
    if len(lines) > max_lines:
        out = out + "\n…"
    return out


def _spec_summary_line(result: RunResult) -> str:
    """One-liner summary of period + covariates + flags, comma-joined."""
    if result.spec is None:
        return ""
    bits: list[str] = [f"period: {result.spec.supported_period_type.value}"]
    if result.spec.required_covariates:
        bits.append("req: " + ", ".join(result.spec.required_covariates))
    if result.spec.additional_continuous_covariates:
        bits.append("acc: " + ", ".join(result.spec.additional_continuous_covariates))
    if result.spec.allow_free_additional_continuous_covariates:
        bits.append("free")
    if result.spec.requires_geo:
        bits.append("geo")
    return "  ·  ".join(bits)


def _build_panel_body(result: RunResult) -> Table:
    """Two-column key/value grid (no border) holding only the populated fields."""
    body = Table.grid(padding=(0, 2))
    body.add_column(style="dim", no_wrap=True)
    body.add_column(overflow="fold")

    spec_line = _spec_summary_line(result)
    if spec_line:
        body.add_row("spec", spec_line)

    cause = _cause_cell(result)
    if cause:
        body.add_row("cause", cause)

    excerpt = _truncate_excerpt(result.error_excerpt) if result.status == RunStatus.fail else ""
    if excerpt:
        body.add_row("error", Text(excerpt, style="red"))

    if result.suggestion and result.status != RunStatus.pass_:
        body.add_row("fix", result.suggestion)

    if result.log_path and result.status == RunStatus.fail:
        body.add_row("log", result.log_path)

    return body


def _render_panel(result: RunResult) -> Panel:
    """Build a status-coloured Panel for a single repo result."""
    icon = _STATUS_ICON[result.status]
    colour = _STATUS_STYLE[result.status]
    duration = f"{result.duration_s:.1f}s" if result.duration_s else "—"
    title = Text.assemble(
        (f"{icon} ", colour),
        (result.repo, "bold"),
        ("  ·  ", "dim"),
        (result.style.value, "dim"),
        ("  ·  ", "dim"),
        (duration, "dim"),
    )
    return Panel(
        _build_panel_body(result),
        title=title,
        title_align="left",
        border_style=colour,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def render_console(report: Report, console: Console | None = None) -> None:
    """Print one Rich panel per repo, ordered failures-first.

    Full suggestions and per-repo logs live in the markdown report; the
    console pulls a short error excerpt + the suggestion inline so a
    single scroll surfaces what needs attention.
    """
    # Cap to 100 cols when no caller-supplied console — keeps panels narrow
    # enough to read without horizontal scrolling, but Rich still honours a
    # narrower terminal when one is detected.
    console = console or Console(width=100)

    header = Text.assemble(
        ("chap-models check: ", "bold"),
        (f"{report.passed} pass", "green"),
        ("  /  ", "dim"),
        (f"{report.failed} fail", "red"),
        ("  /  ", "dim"),
        (f"{report.skipped} skip", "yellow"),
        ("  ", ""),
        (f"({report.total} total)", "dim"),
    )

    ordered = sorted(report.repos, key=lambda r: (_STATUS_ORDER[r.status], r.repo.lower()))
    panels = [_render_panel(r) for r in ordered]

    console.print(header)
    console.print(Group(*panels))
    console.print("[dim]Full suggestions and per-repo logs are in `report.md` / `report.json` under the workdir.[/]")


def _md_cell(text: str | None, *, default: str = "") -> str:
    """Escape a value for safe inclusion in a GitHub-flavoured markdown table cell.

    Replaces newlines with ``<br>`` (so multi-line covariate lists stay
    readable) and backslash-escapes ``|`` so a literal pipe in a note,
    failure message, or covariate name doesn't slice the row into extra
    columns. Falls back to ``default`` when text is empty.
    """
    if text is None or text == "":
        return default
    return text.replace("|", r"\|").replace("\n", "<br>")


def render_markdown(report: Report) -> str:
    """Return a Markdown report (one summary line + one table)."""
    lines: list[str] = []
    lines.append(f"# chap-models check ({report.started_at} → {report.finished_at})")
    lines.append("")
    if report.chap_core_version:
        lines.append(f"chap-core: `{report.chap_core_version}`")
        lines.append("")
    lines.append(f"**{report.passed} pass · {report.failed} fail · {report.skipped} skip** of {report.total} repos.")
    lines.append("")
    lines.append("| Repo | Style | Period | Covariates | Status | Cause / note | Suggested fix | Time |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | ---: |")
    for r in report.repos:
        lines.append(
            "| {repo} | {style} | {period} | {cov} | {status} | {cause} | {sug} | {dur} |".format(
                repo=f"[`{r.repo}`](https://github.com/chap-models/{r.repo})",
                style=_md_cell(r.style.value),
                period=_md_cell(_period_cell(r)),
                cov=_md_cell(_covariates_cell(r), default="—"),
                status=_md_cell(r.status.value),
                cause=_md_cell(_cause_cell(r)),
                sug=_md_cell(r.suggestion),
                dur=f"{r.duration_s:.1f}s" if r.duration_s else "",
            )
        )
    return "\n".join(lines) + "\n"


def write_outputs(report: Report, workdir: Path) -> tuple[Path, Path]:
    """Write ``report.md`` and ``report.json`` under ``workdir``. Returns both paths."""
    workdir.mkdir(parents=True, exist_ok=True)
    md_path = workdir / "report.md"
    json_path = workdir / "report.json"
    md_path.write_text(render_markdown(report))
    json_path.write_text(report.model_dump_json(indent=2))
    return md_path, json_path


def write_snapshot(report: Report, path: Path) -> Path:
    """Write a JSON snapshot of the report to ``path`` (commit-friendly)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n")
    return path


_STATUS_INDICATOR = {
    RunStatus.pass_: ("[green]✓[/]", "pass"),
    RunStatus.fail: ("[red]✗[/]", "fail"),
    RunStatus.skip: ("[yellow]–[/]", "skip"),
}


def _humanize_age(when_iso: str | None) -> str:
    """Return e.g. ``"2 hours ago"`` for an ISO-8601 timestamp."""
    if not when_iso:
        return ""
    try:
        when = datetime.fromisoformat(when_iso)
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = datetime.now(tz=timezone.utc) - when
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    if seconds < 604_800:
        return f"{seconds // 86_400}d ago"
    if seconds < 2_592_000:
        return f"{seconds // 604_800}w ago"
    return f"{seconds // 2_592_000}mo ago"


def _staleness_colour(when_iso: str | None) -> str:
    """Pick a colour for the age display: green <1d, yellow <7d, red older / unknown."""
    if not when_iso:
        return "red"
    try:
        when = datetime.fromisoformat(when_iso)
    except ValueError:
        return "red"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(tz=timezone.utc) - when).total_seconds())
    if seconds < 86_400:
        return "green"
    if seconds < 604_800:
        return "yellow"
    return "red"


def render_list(
    repos: list[RepoInfo],
    snapshot: Report | None,
    console: Console | None = None,
) -> None:
    """Render discovered repos joined with their last-known status from ``snapshot``.

    Each repo shows a single-character status indicator (``✓`` / ``✗`` /
    ``–`` / ``?``) plus the failure bucket when applicable.
    """
    console = console or Console(width=160)

    by_name: dict[str, RunResult] = {}
    if snapshot is not None:
        for r in snapshot.repos:
            by_name[r.repo.lower()] = r

    # Use the OLDEST per-row checked_at for the staleness indicator. A
    # `--repo X` rerun would otherwise update report.finished_at and make
    # every untouched row look freshly swept. For legacy snapshots that
    # predate the per-row field, _merge_snapshots backfills checked_at to
    # the old report.finished_at on existing rows. We additionally treat
    # any *currently* unstamped row as if it carried snapshot.finished_at,
    # so a freshly-loaded legacy snapshot (no merge yet) doesn't let one
    # missing-checked_at row mask staleness either.
    sweep_when: str | None = None
    if snapshot is not None:
        row_times: list[str] = []
        any_unstamped = False
        for r in snapshot.repos:
            if r.checked_at:
                row_times.append(r.checked_at)
            else:
                any_unstamped = True
        if any_unstamped:
            row_times.append(snapshot.finished_at)
        sweep_when = min(row_times) if row_times else snapshot.finished_at
    title = f"chap-models repos ({len(repos)})"
    if sweep_when:
        age = _humanize_age(sweep_when)
        colour = _staleness_colour(sweep_when)
        title += f"  ·  oldest row: {sweep_when}  ([{colour}]{age}[/])"
    else:
        title += "  ·  [red]no snapshot yet — run `chap-models-checker run` to populate[/]"

    table = Table(title=title, header_style="bold", show_lines=False)
    table.add_column("", justify="center", no_wrap=True)
    table.add_column("Repo", no_wrap=True)
    table.add_column("Style", no_wrap=True)
    table.add_column("Last status", no_wrap=True)
    table.add_column("Failure / note", overflow="fold", min_width=24)
    table.add_column("Description", overflow="fold")

    counts: dict[str, int] = {"pass": 0, "fail": 0, "skip": 0, "untested": 0}
    for repo in repos:
        result = by_name.get(repo.name.lower())
        if result is None:
            indicator = "[dim]?[/]"
            last_status = "[dim]untested[/]"
            failure_note = ""
            style = ""
            counts["untested"] += 1
        else:
            indicator, label = _STATUS_INDICATOR[result.status]
            counts[label] += 1
            colour = {"pass": "green", "fail": "red", "skip": "yellow"}[label]
            last_status = f"[{colour}]{label}[/]"
            failure_note = _cause_cell(result)
            style = result.style.value
        table.add_row(
            indicator,
            repo.name,
            style,
            last_status,
            failure_note,
            (repo.description or "")[:140],
        )

    console.print(table)
    summary_bits = [
        f"[green]{counts['pass']} pass[/]",
        f"[red]{counts['fail']} fail[/]",
        f"[yellow]{counts['skip']} skip[/]",
        f"[dim]{counts['untested']} untested[/]",
    ]
    console.print("  ·  ".join(summary_bits))
