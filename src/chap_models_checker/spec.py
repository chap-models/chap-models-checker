"""Classify a repo and extract a ModelSpec.

Two paths:

* MLproject: fetch the raw YAML and parse it.
* chapkit:   the caller boots the service via ``chapkit_service`` and
  passes the resulting ``/info`` + ``$schema`` payloads here.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import httpx
import yaml

from chap_models_checker.models import ModelSpec, ModelStyle, PeriodType, RepoInfo

logger = logging.getLogger(__name__)


_PERIOD_ALIASES = {
    "monthly": PeriodType.month,
    "weekly": PeriodType.week,
    "yearly": PeriodType.year,
    "annual": PeriodType.year,
}


def _coerce_period(value: str) -> PeriodType:
    """Map both chapkit (`monthly`/`weekly`) and chap-core (`month`/`week`) spellings."""
    v = value.strip().lower()
    if v in _PERIOD_ALIASES:
        return _PERIOD_ALIASES[v]
    try:
        return PeriodType(v)
    except ValueError:
        return PeriodType.any


def _coerce_str_list(value: object, *, field_name: str, source: str) -> list[str]:
    """Coerce a raw spec field into ``list[str]``, refusing scalar coercion.

    Naïve ``list(value)`` turns a string like ``"rainfall"`` into
    ``["r", "a", "i", "n", ...]``, which would then drive synth column
    generation off a pile of single-character "covariates" before
    chap-core ever got to flag the actual schema error. Accept lists
    only; log a warning and return ``[]`` for anything else so the
    spec-only report stays honest and the live chap-eval run surfaces
    the real validation message.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    logger.warning(
        "ignoring malformed %s in %s (expected list, got %s: %r)",
        field_name,
        source,
        type(value).__name__,
        value,
    )
    return []


# ---------------------------- classification --------------------------------


def detect_style(repo: RepoInfo, timeout: float = 15.0) -> ModelStyle:
    """Classify the repo by inspecting its default-branch contents.

    Returns ``mlproject`` when an ``MLproject`` file is present, ``chapkit``
    when its ``pyproject.toml`` lists chapkit as a dependency, and ``unknown``
    otherwise.

    Only treats a 404 as "file absent". Other non-200 responses (raw GitHub
    rate-limit 403s, 5xx, redirects we can't follow) are raised so the
    caller records them as ``spec_fetch_failed`` instead of silently
    blaming the repo.
    """
    base = repo.raw_url_prefix
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        ml = client.head(f"{base}/MLproject")
        if ml.status_code == 200:
            return ModelStyle.mlproject
        if ml.status_code != 404:
            ml.raise_for_status()

        pyproj = client.get(f"{base}/pyproject.toml")
        if pyproj.status_code == 200:
            if "chapkit" in pyproj.text.lower():
                return ModelStyle.chapkit
        elif pyproj.status_code != 404:
            pyproj.raise_for_status()

    return ModelStyle.unknown


# ---------------------------- MLproject path --------------------------------


def _entry_points_use_polygons(data: dict[str, Any]) -> bool:
    """True when any MLproject entry_point command references the ``{polygons}`` placeholder.

    chap-core's ``ModelTemplateConfigV2`` rejects ``requires_geo`` on
    MLproject (it's only a chapkit field), so an MLproject author can't
    declare a polygon dependency in the spec. They signal it implicitly
    by writing ``Rscript train.R ... {polygons}`` in the command. We
    treat that placeholder as the canonical signal so synth data still
    gets a ``.geojson`` and chap-core can substitute the path at runtime.
    """
    entry_points = data.get("entry_points") or {}
    if not isinstance(entry_points, dict):
        return False
    for ep in entry_points.values():
        if not isinstance(ep, dict):
            continue
        command = ep.get("command")
        if isinstance(command, str) and "{polygons}" in command:
            return True
    return False


def fetch_mlproject_spec(repo: RepoInfo, timeout: float = 30.0) -> ModelSpec:
    """Fetch raw MLproject and map it onto ModelSpec."""
    url = f"{repo.raw_url_prefix}/MLproject"
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data: dict[str, Any] = yaml.safe_load(resp.text) or {}

    period_raw = data.get("supported_period_type") or data.get("period") or "any"
    period = _coerce_period(str(period_raw))

    docker_env = data.get("docker_env") or {}
    docker_image = docker_env.get("image") if isinstance(docker_env, dict) else None

    requires_geo = bool(data.get("requires_geo", False)) or _entry_points_use_polygons(data)

    return ModelSpec(
        name=str(data.get("name") or repo.name),
        style=ModelStyle.mlproject,
        target=str(data.get("target", "disease_cases")),
        required_covariates=_coerce_str_list(
            data.get("required_covariates"),
            field_name="required_covariates",
            source=f"MLproject for {repo.name}",
        ),
        additional_continuous_covariates=[],  # MLproject doesn't carry these
        allow_free_additional_continuous_covariates=bool(
            data.get("allow_free_additional_continuous_covariates", False)
        ),
        supported_period_type=period,
        requires_geo=requires_geo,
        adapters=data.get("adapters") or None,
        docker_image=str(docker_image) if docker_image else None,
    )


# ---------------------------- chapkit path ----------------------------------


def clone_repo(
    repo: RepoInfo,
    dest: Path,
    *,
    fetch_timeout: float = 300.0,
    clone_timeout: float = 600.0,
) -> Path:
    """Clone (or refresh) the repo to ``dest``. Returns the directory.

    If ``dest`` already exists we try ``git fetch`` + ``git reset --hard
    FETCH_HEAD`` first (cheap). When either step fails (corrupt working
    tree, network blip, default branch renamed, …) we nuke ``dest`` and
    do a full clone instead of silently testing stale code against the
    current chap-models repo. A failing clone propagates as
    ``CalledProcessError`` so the caller's per-repo handler can record
    it as ``spec_fetch_failed``.

    Timeouts on each git invocation, plus FileNotFoundError handling,
    keep a wedged network or a missing ``git`` binary from stalling /
    aborting the whole sweep — both surface as ``CalledProcessError``
    that ``_process_chapkit`` already catches.
    """
    import shutil

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        try:
            _git_run(
                ["git", "-C", str(dest), "fetch", "--depth=1", "origin", repo.default_branch],
                timeout=fetch_timeout,
            )
            _git_run(
                ["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"],
                timeout=fetch_timeout,
            )
            return dest
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace").strip()[:200] if exc.stderr else str(exc)
            logger.warning("refresh of existing clone %s failed (%s) — re-cloning", dest, stderr)
            shutil.rmtree(dest, ignore_errors=True)

    _git_run(
        ["git", "clone", "--depth=1", "--branch", repo.default_branch, repo.clone_url, str(dest)],
        timeout=clone_timeout,
    )
    return dest


def _git_run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    """Run a git subprocess and translate FileNotFoundError / TimeoutExpired into CalledProcessError.

    ``_process_chapkit`` already converts ``CalledProcessError`` to a
    per-repo ``spec_fetch_failed`` row; raising the same type here lets
    a missing git binary or a hung network clone flow through the same
    path instead of escaping into the main loop.
    """
    try:
        return subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        # git binary not on PATH — present this as a CalledProcessError
        # carrying a descriptive stderr so the per-repo handler renders
        # it like any other git failure.
        raise subprocess.CalledProcessError(127, cmd, b"", b"git CLI not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise subprocess.CalledProcessError(
            124, cmd, b"", f"git operation timed out after {timeout:.0f}s".encode()
        ) from exc


def chapkit_spec_from_info(
    repo_name: str,
    info: dict[str, Any],
    schema: dict[str, Any],
) -> ModelSpec:
    """Build a ModelSpec from a chapkit service's ``/info`` + ``$schema`` payloads."""
    period_raw = info.get("period_type") or info.get("supported_period_type") or info.get("period") or "any"
    period = _coerce_period(str(period_raw))

    acc_default: list[str] = []
    props = (schema or {}).get("properties", {})
    acc_field = props.get("additional_continuous_covariates") if isinstance(props, dict) else None
    if isinstance(acc_field, dict):
        default = acc_field.get("default")
        if isinstance(default, list):
            acc_default = [str(c) for c in default]

    return ModelSpec(
        name=str(info.get("id") or info.get("name") or repo_name),
        style=ModelStyle.chapkit,
        target=str(info.get("target", "disease_cases")),
        required_covariates=_coerce_str_list(
            info.get("required_covariates"),
            field_name="required_covariates",
            source=f"chapkit /info for {repo_name}",
        ),
        additional_continuous_covariates=acc_default,
        allow_free_additional_continuous_covariates=bool(
            info.get("allow_free_additional_continuous_covariates", False)
        ),
        supported_period_type=period,
        requires_geo=bool(info.get("requires_geo", False)),
    )
