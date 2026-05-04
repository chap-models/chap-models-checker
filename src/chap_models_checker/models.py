"""Pydantic data models shared across the checker."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ModelStyle(str, Enum):
    """How the repo is meant to be invoked under chap-core."""

    mlproject = "mlproject"
    chapkit = "chapkit"
    unknown = "unknown"


class PeriodType(str, Enum):
    """Mirror of chap_core.model_spec.PeriodType."""

    week = "week"
    month = "month"
    year = "year"
    any = "any"


class RepoInfo(BaseModel):
    """A repo discovered under github.com/chap-models."""

    name: str
    full_name: str
    default_branch: str
    archived: bool = False
    description: str | None = None
    html_url: str
    clone_url: str

    @property
    def raw_url_prefix(self) -> str:
        """Base URL for fetching files from the default branch via raw.githubusercontent."""
        return f"https://raw.githubusercontent.com/{self.full_name}/{self.default_branch}"


class ModelSpec(BaseModel):
    """The bits of MLproject / chapkit /api/v1/info we care about for picking inputs."""

    name: str
    style: ModelStyle
    target: str = "disease_cases"
    required_covariates: list[str] = Field(default_factory=list)
    additional_continuous_covariates: list[str] = Field(default_factory=list)
    allow_free_additional_continuous_covariates: bool = False
    supported_period_type: PeriodType = PeriodType.any
    requires_geo: bool = False
    adapters: dict[str, str] | None = None
    docker_image: str | None = None  # MLproject `docker_env.image` (used for cross-arch pre-pull)


class RunStatus(str, Enum):
    """Final classification for a repo run."""

    pass_ = "pass"
    fail = "fail"
    skip = "skip"


class FailureCategory(str, Enum):
    """Coarse buckets we map run logs into."""

    no_mlproject = "no_mlproject"
    no_pyproject = "no_pyproject"
    invalid_mlproject = "invalid_mlproject"
    spec_fetch_failed = "spec_fetch_failed"
    docker_pull_failed = "docker_pull_failed"
    docker_build_failed = "docker_build_failed"
    docker_image_missing_runtime = "docker_image_missing_runtime"
    schema_mismatch = "schema_mismatch"
    missing_covariate = "missing_covariate"
    model_runtime_error = "model_runtime_error"
    prediction_length = "prediction_length"
    timeout = "timeout"
    nonzero_exit = "nonzero_exit"
    no_output_file = "no_output_file"
    spec_only = "spec_only"
    other = "other"


class RunResult(BaseModel):
    """Outcome of a single repo run."""

    repo: str
    style: ModelStyle
    spec: ModelSpec | None = None
    status: RunStatus
    failure: FailureCategory | None = None
    duration_s: float = 0.0
    log_path: str | None = None
    dataset_path: str | None = None
    output_file: str | None = None
    error_excerpt: str | None = None
    note: str | None = None
    suggestion: str | None = None
    platform_override: str | None = None  # set when we forced linux/amd64 to make a docker image work
    # Persisted so `reclassify` can re-bucket timeout / no_output_file accurately
    # without having to rerun chap eval.
    returncode: int | None = None
    timed_out: bool = False
    # ISO-8601 timestamp of the chap-eval run that produced this row. On a
    # filtered run (`--repo X`) this is what tells `list` that other rows
    # are stale even though the report-level finished_at was just updated
    # by the merge.
    checked_at: str | None = None


class Report(BaseModel):
    """Wraps a full checker run."""

    chap_core_version: str | None = None
    repos: list[RunResult] = Field(default_factory=list)
    started_at: str
    finished_at: str

    @property
    def total(self) -> int:
        return len(self.repos)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.repos if r.status == RunStatus.pass_)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.repos if r.status == RunStatus.fail)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.repos if r.status == RunStatus.skip)


# Convenience for typer choices
DataStrategy = Literal["curated", "synthetic", "hybrid"]
OnlyStyle = Literal["all", "mlproject", "chapkit"]
