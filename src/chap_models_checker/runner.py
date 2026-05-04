"""Run ``chap eval`` for a single repo via ``uvx --from chap-core``."""

from __future__ import annotations

import logging
import os
import platform
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel

from chap_models_checker.models import FailureCategory, ModelSpec, ModelStyle

logger = logging.getLogger(__name__)


def kill_process_tree(proc: subprocess.Popen[bytes], grace: float = 5.0) -> None:
    """SIGTERM (then SIGKILL) the process group ``proc`` runs in.

    Used on timeout so any helper subprocesses chap eval spawned (uv, docker
    CLI, the Python-docker helper, fastapi, …) get reaped together. Requires
    ``Popen(start_new_session=True)``.
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=grace)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def _running_container_ids(image: str | None = None) -> set[str]:
    """Snapshot the IDs of running docker containers, optionally filtered by image.

    When ``image`` is given, only containers spawned from that image are
    returned (`docker ps --filter ancestor=<image>`). Without the filter,
    we'd risk stopping unrelated containers on a shared workstation when
    cleaning up after a timeout.

    Returns an empty set when docker isn't available or the call fails — the
    caller treats this as best-effort cleanup, not a hard requirement.
    """
    cmd = ["docker", "ps", "--quiet", "--no-trunc"]
    if image:
        cmd.extend(["--filter", f"ancestor={image}"])
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    if out.returncode != 0:
        return set()
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _stop_containers(container_ids: set[str], stop_timeout: int = 5) -> None:
    """``docker stop`` each container, swallowing already-gone errors."""
    if not container_ids:
        return
    try:
        subprocess.run(
            ["docker", "stop", "-t", str(stop_timeout), *container_ids],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def host_is_arm() -> bool:
    """True when this host's CPU is arm-family (Apple Silicon, etc.)."""
    return platform.machine().lower() in {"arm64", "aarch64"}


def pre_pull_docker_image(image: str, *, platform_arg: str = "linux/amd64", timeout: float = 600.0) -> tuple[bool, str]:
    """Pre-pull ``image`` so a later ``docker run`` resolves to the cached manifest.

    Pulls with ``--platform=<platform_arg>`` so the daemon caches the right
    arch under Rosetta. Returns (ok, message). Failure is non-fatal —
    chap eval may still find a matching manifest on its own; we just paper
    over the common Apple-Silicon case where the image only ships an amd64
    manifest list entry. Missing docker CLI, hung pulls, and registry
    errors are all reported as ``(False, ...)`` instead of escaping.
    """
    if not image or "/" in image and image.split("/")[0] in {"localhost", "127.0.0.1"}:
        return False, f"skipping pre-pull for {image!r}"
    try:
        res = subprocess.run(
            ["docker", "pull", "--platform", platform_arg, image],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return False, "pre-pull skipped: docker CLI not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"pre-pull failed for {image} ({platform_arg}): timed out after {timeout:.0f}s"
    if res.returncode == 0:
        return True, f"pre-pulled {image} ({platform_arg})"
    err = (res.stderr or res.stdout).strip().splitlines()[-1] if (res.stderr or res.stdout) else ""
    return False, f"pre-pull failed for {image} ({platform_arg}): {err[:200]}"


class RunOutcome(BaseModel):
    """Raw output of one ``chap eval`` invocation."""

    cmd: list[str]
    returncode: int
    duration_s: float
    log_path: Path
    output_file: Path
    output_exists: bool
    timed_out: bool
    pre_pull_note: str | None = None
    platform_override: str | None = None


def build_chap_eval_cmd(
    *,
    chap_core_version: str | None,
    model_path: str,
    style: ModelStyle,
    dataset_csv: Path,
    output_file: Path,
    n_periods: int = 3,
    n_splits: int = 3,
    stride: int = 1,
) -> list[str]:
    """Assemble the ``uvx --from chap-core[==X.Y] chap eval ...`` invocation."""
    chap_core = "chap-core" if chap_core_version is None else f"chap-core=={chap_core_version}"
    cmd = [
        "uvx",
        "--from",
        chap_core,
        "chap",
        "eval",
        "--model-name",
        model_path,
        "--dataset-csv",
        str(dataset_csv),
        "--output-file",
        str(output_file),
        "--backtest-params.n-periods",
        str(n_periods),
        "--backtest-params.n-splits",
        str(n_splits),
        "--backtest-params.stride",
        str(stride),
    ]
    if style == ModelStyle.chapkit:
        cmd.append("--run-config.is-chapkit-model")
    return cmd


def run_chap_eval(
    *,
    chap_core_version: str | None,
    model_path: str,
    style: ModelStyle,
    dataset_csv: Path,
    output_file: Path,
    log_path: Path,
    timeout: float,
    n_periods: int = 3,
    n_splits: int = 3,
    stride: int = 1,
    docker_image: str | None = None,
    force_amd64_on_arm: bool = True,
) -> RunOutcome:
    """Run ``chap eval`` and tee combined stdout/stderr to ``log_path``.

    When ``docker_image`` is supplied (MLproject style) and the host is arm,
    we pre-pull it with ``--platform=linux/amd64`` so chap-core's
    ``python-docker`` calls find a cached amd64 manifest and the daemon
    runs it under Rosetta. ``DOCKER_DEFAULT_PLATFORM`` is also set in the
    subprocess env as a belt-and-braces measure for any docker CLI calls.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Wipe any leftover eval.nc from a previous run before invoking chap eval —
    # otherwise a clean rc=0 + the stale file would falsely register as PASS
    # even when the new run produced nothing. `output_exists` after the run
    # then strictly means "this invocation wrote it".
    if output_file.exists():
        output_file.unlink()

    cmd = build_chap_eval_cmd(
        chap_core_version=chap_core_version,
        model_path=model_path,
        style=style,
        dataset_csv=dataset_csv,
        output_file=output_file,
        n_periods=n_periods,
        n_splits=n_splits,
        stride=stride,
    )

    env = os.environ.copy()
    pre_pull_note: str | None = None
    platform_override: str | None = None
    if docker_image and force_amd64_on_arm and host_is_arm():
        env.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")
        platform_override = "linux/amd64"
        _ok, msg = pre_pull_docker_image(docker_image)
        pre_pull_note = msg
        logger.info(msg)

    timed_out = False
    start = time.time()
    with log_path.open("w") as logf:
        if pre_pull_note:
            logf.write(f"# {pre_pull_note}\n")
        if platform_override:
            logf.write(f"# DOCKER_DEFAULT_PLATFORM={platform_override}\n")
        logf.write(f"$ {shlex.join(cmd)}\n\n")
        logf.flush()
        # Run chap eval in its own process group so an enforced timeout can
        # SIGKILL the whole tree, not just the uvx wrapper. subprocess.run()
        # with timeout only signals the direct child, leaving any sub-uv /
        # docker-CLI / Python-spawned helpers running and consuming resources
        # (and CPU under emulation) into the next repo's slot.
        #
        # Docker containers chap-core launches through the python-docker
        # library are owned by the daemon and survive the process kill, so
        # we additionally snapshot containers running the model's image
        # before the run and stop any new ones that appear during it. The
        # ancestor filter scopes cleanup to containers chap eval likely
        # started — without it, a shared workstation / CI worker could lose
        # unrelated containers that started in the same window.
        containers_before = _running_container_ids(docker_image) if docker_image else set()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError:
            # uvx not on PATH — surface as a per-repo failure marker so the
            # sweep continues and classify_failure / suggest_fix can speak to
            # it; without this the FileNotFoundError would escape into the
            # main loop and abort everything else.
            logf.write("\n[chap-models-checker] uvx not on PATH — install uv (https://docs.astral.sh/uv/)\n")
            return RunOutcome(
                cmd=cmd,
                returncode=127,
                duration_s=time.time() - start,
                log_path=log_path,
                output_file=output_file,
                output_exists=False,
                timed_out=False,
                pre_pull_note=pre_pull_note,
                platform_override=platform_override,
            )
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(proc)
            if docker_image:
                new_containers = _running_container_ids(docker_image) - containers_before
                if new_containers:
                    _stop_containers(new_containers)
                    logf.write(
                        f"\n[chap-models-checker] stopped {len(new_containers)} leftover "
                        f"container(s) for image {docker_image}\n"
                    )
            returncode = -1
            logf.write(f"\n[chap-models-checker] TIMEOUT after {timeout}s\n")

    duration = time.time() - start
    return RunOutcome(
        cmd=cmd,
        returncode=returncode,
        duration_s=duration,
        log_path=log_path,
        output_file=output_file,
        output_exists=output_file.exists() and output_file.stat().st_size > 0,
        timed_out=timed_out,
        pre_pull_note=pre_pull_note,
        platform_override=platform_override,
    )


# ---------------------------- failure classifier ----------------------------


# Order matters: more specific patterns must come first.
_PATTERNS: list[tuple[re.Pattern[str], FailureCategory]] = [
    (re.compile(r"No MLproject file found", re.IGNORECASE), FailureCategory.no_mlproject),
    (re.compile(r"validation errors? for ModelTemplateConfigV2", re.IGNORECASE), FailureCategory.invalid_mlproject),
    (
        re.compile(r"\d+ validation errors? for MLServiceInfo|extra_forbidden.*MLServiceInfo", re.IGNORECASE),
        FailureCategory.schema_mismatch,
    ),
    (
        re.compile(
            r"Error response from daemon: pull access denied|"
            r"pull access denied|denied: requested access|manifest unknown|"
            r"no matching manifest for|"
            r"ImageNotFound|"
            r"repository does not exist",
            re.IGNORECASE,
        ),
        FailureCategory.docker_pull_failed,
    ),
    (re.compile(r"failed to build:|ERROR: failed to solve:", re.IGNORECASE), FailureCategory.docker_build_failed),
    (
        re.compile(
            r"Rscript .* return code 127|"
            r"/bin/sh: Rscript: command not found|"
            r"could not find function|"
            r"there is no package called",
            re.IGNORECASE,
        ),
        FailureCategory.docker_image_missing_runtime,
    ),
    (
        re.compile(r"uvx not on PATH", re.IGNORECASE),
        FailureCategory.other,
    ),
    (
        re.compile(r"prediction length|less than the model's minimum prediction length", re.IGNORECASE),
        FailureCategory.prediction_length,
    ),
    (
        re.compile(
            r"KeyError: \"\['[^']+'\] not in index\"|"
            r"column .* not found|"
            r"not present in.*data|"
            r"unknown covariate",
            re.IGNORECASE,
        ),
        FailureCategory.missing_covariate,
    ),
    (re.compile(r"ModelFailedException", re.IGNORECASE), FailureCategory.model_runtime_error),
]


def _strip_runner_metadata(log_text: str) -> str:
    """Drop the leading ``# ...`` block we wrote ourselves.

    Without this, our pre-pull / DOCKER_DEFAULT_PLATFORM lines can match
    later regex patterns (e.g. "pull access denied" inside a pre-pull
    warning would otherwise look like a real ``docker_pull_failed``).
    """
    lines = log_text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].startswith("#") or not lines[i].strip()):
        i += 1
    return "\n".join(lines[i:])


def classify_failure(outcome: RunOutcome, log_text: str) -> FailureCategory:
    """Best-effort bucketing of the failure mode from log content."""
    if outcome.timed_out:
        return FailureCategory.timeout
    body = _strip_runner_metadata(log_text)
    for pattern, cat in _PATTERNS:
        if pattern.search(body):
            return cat
    if outcome.returncode == 0 and not outcome.output_exists:
        return FailureCategory.no_output_file
    return FailureCategory.nonzero_exit


# ---------------------------- fix suggestions ------------------------------


_SUGGESTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"uvx not on PATH", re.IGNORECASE),
        "uvx (part of uv) is not installed on the runner. Install via "
        "https://docs.astral.sh/uv/ or `pipx install uv`, then retry.",
    ),
    (
        re.compile(r"Rscript: command not found", re.IGNORECASE),
        "Add a Dockerfile based on `ghcr.io/dhis2-chap/chapkit-r-inla` (or chapkit-r). "
        "The model shells out to Rscript, so the chapkit-py base / `uv run` won't work.",
    ),
    (
        re.compile(r"\d+ validation errors? for MLServiceInfo", re.IGNORECASE),
        "chap-core ↔ chapkit schema mismatch: chap-core's `MLServiceInfo` doesn't match "
        "the JSON the running chapkit service returns. Bump chap-core to a version that "
        "matches the chapkit version the model pins, or bump chapkit in the model.",
    ),
    (
        re.compile(r"No MLproject file found", re.IGNORECASE),
        "Add an MLproject file at the repo root or convert to chapkit. "
        "See chap-core docs / `chapkit init` for templates.",
    ),
    (
        re.compile(r"pull access denied|denied: requested access|manifest unknown", re.IGNORECASE),
        "Make the model's docker image public on ghcr.io, or switch to a chapkit-images "
        "base image (`chapkit-py` / `chapkit-r` / `chapkit-r-inla`).",
    ),
    (
        re.compile(r"KeyError: \"\['(?P<col>[^']+)'\] not in index\"", re.IGNORECASE),
        "Model expects column that's not in the dataset. Either provide a CSV with that "
        "column, or pass `--data-source-mapping` to map an existing column.",
    ),
    (
        re.compile(r"prediction length|n_periods", re.IGNORECASE),
        "Reduce `--backtest-params.n-periods`, or widen `min_prediction_length` / "
        "`max_prediction_length` in MLproject / MLServiceInfo.",
    ),
    (
        re.compile(
            r"unknown covariate|column .* not found|not present in.*data|"
            r"KeyError.*'(rainfall|mean_temperature|population|precipitation|temperature)'",
            re.IGNORECASE,
        ),
        "Declare the column in MLproject `required_covariates` (or chapkit "
        "`additional_continuous_covariates` default), or use `--data-source-mapping` so "
        "the model finds the column under its expected name.",
    ),
]


_FIXED_SUGGESTIONS: dict[FailureCategory, str] = {
    FailureCategory.no_mlproject: (
        "Add an MLproject at the repo root, or convert to chapkit (scaffold via `uvx --from chapkit chapkit init`)."
    ),
    FailureCategory.invalid_mlproject: (
        "MLproject schema doesn't match chap-core's `ModelTemplateConfigV2`. Common "
        "causes: stringified value where chap-core expects a list (e.g. "
        '`required_covariates: "foo"` instead of `[foo]`), or extra/typo\'d top-level keys.'
    ),
    FailureCategory.schema_mismatch: (
        "chap-core ↔ chapkit schema mismatch: chap-core's pinned `MLServiceInfo` model "
        "doesn't match the JSON the running chapkit service returns. Bump chap-core to a "
        "version compatible with the chapkit version pinned by the model, or update the "
        "model to a chapkit version chap-core supports."
    ),
    FailureCategory.docker_pull_failed: (
        "MLproject `docker_env.image` is private, missing, or arch-incompatible. "
        "Push the image to ghcr.io public, switch to a chapkit-images base "
        "(`chapkit-py` / `chapkit-r` / `chapkit-r-inla`), or pin a multi-arch tag."
    ),
    FailureCategory.docker_build_failed: (
        "Docker build failed. On Apple Silicon, pin `--platform=linux/amd64` in the "
        "Dockerfile FROM line for R-INLA bases (R-INLA wheels are amd64-only)."
    ),
    FailureCategory.docker_image_missing_runtime: (
        "Model's container is missing R / required R packages. Use chapkit-r or "
        "chapkit-r-inla as the base image, and `R -e 'install.packages(...)'` (or apt) "
        "the missing libraries during build."
    ),
    FailureCategory.timeout: (
        "Eval exceeded the timeout. Pre-pull the docker image, reduce backtest splits, or pass `--timeout` higher."
    ),
    FailureCategory.no_output_file: (
        "chap eval returned 0 but didn't write the .nc file — check the model's predict step."
    ),
    FailureCategory.prediction_length: (
        "Reduce `--backtest-params.n-periods`, or widen `min_prediction_length` / "
        "`max_prediction_length` in MLproject / MLServiceInfo."
    ),
    FailureCategory.missing_covariate: (
        "Declare the column in MLproject `required_covariates` (or chapkit "
        "`additional_continuous_covariates` default), or use `--data-source-mapping` so "
        "the model finds the column under its expected name."
    ),
    FailureCategory.model_runtime_error: (
        "Model's train/predict script failed at runtime. Inspect the run.log "
        "for the underlying error — common causes: undeclared covariates, "
        "missing R/Python deps, or data-shape assumptions in the model script."
    ),
}


def suggest_fix(failure: FailureCategory, log_text: str) -> str | None:
    """Map a failure (and recent log content) to a concrete fix suggestion."""
    body = _strip_runner_metadata(log_text)
    if failure in _FIXED_SUGGESTIONS:
        return _FIXED_SUGGESTIONS[failure]
    for pattern, msg in _SUGGESTION_PATTERNS:
        if pattern.search(body):
            return msg
    if failure == FailureCategory.nonzero_exit:
        return _FIXED_SUGGESTIONS[FailureCategory.model_runtime_error]
    return None


def excerpt_error(log_text: str, max_lines: int = 8) -> str:
    """Pull a small tail of error-ish lines for the report."""
    lines = log_text.strip().splitlines()
    tail = lines[-40:] if len(lines) > 40 else lines
    interesting = [
        ln for ln in tail if any(tok in ln.lower() for tok in ("error", "traceback", "exception", "failed", "fatal"))
    ]
    chosen = interesting[-max_lines:] if interesting else tail[-max_lines:]
    return "\n".join(chosen)[:1200]


def _spec_summary(spec: ModelSpec | None) -> str:
    if spec is None:
        return ""
    bits = [f"period={spec.supported_period_type.value}"]
    if spec.required_covariates:
        bits.append("req=" + ",".join(spec.required_covariates))
    if spec.requires_geo:
        bits.append("geo")
    return " ".join(bits)
