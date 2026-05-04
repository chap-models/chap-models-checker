"""Spin up a chapkit service from a local clone and tear it back down.

Two layouts are supported:

* **Flat layout** — ``main.py`` at the repo root (chap-core's
  ``ChapkitServiceManager`` assumes this). Boot via ``uv run fastapi dev``.
* **src layout** — package under ``src/<pkg>/`` with a ``__main__.py``.
  Boot via ``uv run python -m <pkg>``; the package is expected to honour
  ``HOST`` / ``PORT`` env vars.

The service URL is passed to ``chap eval`` (with
``--run-config.is-chapkit-model``) instead of the directory path, so the
runner does not have to share chap-core's flat-layout assumption.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

import httpx

from chap_models_checker.runner import kill_process_tree

logger = logging.getLogger(__name__)


class ChapkitServiceError(RuntimeError):
    """Service failed to install / boot / become healthy."""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_healthy(url: str, proc: subprocess.Popen[Any], timeout: float) -> None:
    """Poll ``{url}/health`` until 200 or process dies. Raises on failure."""
    deadline = time.time() + timeout
    with httpx.Client(timeout=2.0) as client:
        while time.time() < deadline:
            if proc.poll() is not None:
                raise ChapkitServiceError(f"chapkit service exited (rc={proc.returncode}) before becoming healthy")
            try:
                if client.get(f"{url}/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
    raise ChapkitServiceError(f"chapkit service at {url} did not become healthy in {timeout:.0f}s")


def _detect_src_package(clone_dir: Path) -> str | None:
    """Return the src-layout package name to launch via ``python -m``, or None."""
    src = clone_dir / "src"
    if not src.is_dir():
        return None
    candidates = [p for p in src.iterdir() if p.is_dir() and (p / "__main__.py").exists()]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].name

    # Multi-package: prefer the one matching pyproject's project.name.
    pyproj = clone_dir / "pyproject.toml"
    if pyproj.exists():
        try:
            data = tomllib.loads(pyproj.read_text())
            name = (data.get("project") or {}).get("name")
            if isinstance(name, str):
                wanted = name.replace("-", "_")
                for c in candidates:
                    if c.name == wanted:
                        return c.name
        except (tomllib.TOMLDecodeError, OSError):
            pass
    return candidates[0].name


def uv_sync(clone_dir: Path, *, timeout: float = 600.0) -> None:
    """Run ``uv sync`` in ``clone_dir``; raise ChapkitServiceError on failure.

    Failures (missing uv binary, hung resolver, non-zero exit) all map to
    ChapkitServiceError so the per-repo handler in ``_process_chapkit``
    catches them — without the wrapper, FileNotFoundError or
    TimeoutExpired would escape and abort the whole sweep.
    """
    try:
        res = subprocess.run(
            ["uv", "sync"],
            cwd=clone_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ChapkitServiceError(f"uv sync failed in {clone_dir.name}: uv CLI not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ChapkitServiceError(f"uv sync timed out after {timeout:.0f}s in {clone_dir.name}") from exc
    if res.returncode != 0:
        raise ChapkitServiceError(f"uv sync failed in {clone_dir.name}: {res.stderr.strip()[:500]}")


CHAPKIT_IMAGES = {
    "py": "ghcr.io/dhis2-chap/chapkit-py:latest",
    "r": "ghcr.io/dhis2-chap/chapkit-r:latest",
    "r-inla": "ghcr.io/dhis2-chap/chapkit-r-inla:latest",
}


# Directory names we skip while scanning a clone for R sources — venvs,
# checkouts, build outputs and generated artifacts. Anything that would
# otherwise inflate the walk and shouldn't influence runtime detection.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".tox",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "target",
        "site",
        "renv",
    }
)


def _has_r_sources(clone_dir: Path) -> bool:
    """Recursive scan for ``*.R`` / ``*.r`` files anywhere in the clone.

    Scoped to skip noisy / generated directories. The previous shallow
    glob only checked ``./``, ``./scripts``, and ``./src`` (depth 1) and
    missed nested layouts like ``src/<pkg>/model/foo.R``, which routed
    those repos to the pure-Python boot path and produced a misleading
    runtime failure.
    """
    for _root, dirs, files in os.walk(clone_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name.endswith((".R", ".r")):
                return True
    return False


def _detect_runtime(clone_dir: Path) -> str:
    """Return ``"py"`` / ``"r"`` / ``"r-inla"`` based on repo contents."""
    dockerfile = clone_dir / "Dockerfile"
    text = ""
    if dockerfile.exists():
        try:
            text = dockerfile.read_text(errors="replace").lower()
        except OSError:
            text = ""
    if "inla" in text or "library(inla)" in text or "fmesher" in text:
        return "r-inla"
    if any(m in text for m in ("chapkit-r", "rocker/", "rscript", "/usr/bin/r")):
        return "r"
    if _has_r_sources(clone_dir):
        # default any-R repo without an obvious Dockerfile clue to r-inla — that
        # base image is a strict superset of r and matches the dominant case.
        return "r-inla"
    return "py"


def needs_docker(clone_dir: Path) -> bool:
    """Heuristic: does this chapkit model need a Docker container to run?

    Returns True when the repo carries R sources or its Dockerfile pulls from
    chapkit-r* / r-inla / similar — these models shell out to ``Rscript`` at
    train time, so booting them with ``uv run`` locally without R installed
    will fail with exit code 127.
    """
    return _detect_runtime(clone_dir) != "py"


def _ensure_dockerfile(clone_dir: Path) -> Path:
    """Return a Dockerfile path, synthesizing one if the repo lacks its own.

    The synthetic Dockerfile uses a ``chapkit-images`` base and tries
    ``uv sync`` so the model's own deps still get installed; this can still
    break if those deps require system packages absent from the base image
    (caller is responsible for surfacing the failure).
    """
    real = clone_dir / "Dockerfile"
    if real.exists():
        return real

    runtime = _detect_runtime(clone_dir)
    base = CHAPKIT_IMAGES[runtime]
    src_pkg = _detect_src_package(clone_dir)
    flat_main = clone_dir / "main.py"
    if src_pkg is not None:
        cmd = f'CMD ["python", "-m", "{src_pkg}"]'
    elif flat_main.exists():
        cmd = 'CMD ["fastapi", "run", "main.py", "--host", "0.0.0.0", "--port", "8000"]'
    else:
        raise ChapkitServiceError(
            f"{clone_dir.name}: synthetic Dockerfile fallback needs main.py or src/<pkg>/__main__.py"
        )

    # Decide the COPY line based on what the repo actually has on disk.
    # `uv.lock*` glob in a single COPY can fail under some BuildKit
    # frontends when the wildcard matches nothing, so we never emit a
    # wildcard against an absent file.
    has_pyproject = (clone_dir / "pyproject.toml").exists()
    has_lock = (clone_dir / "uv.lock").exists()
    deps_copy_lines: list[str] = []
    if has_pyproject and has_lock:
        deps_copy_lines.append("COPY pyproject.toml uv.lock ./")
    elif has_pyproject:
        deps_copy_lines.append("COPY pyproject.toml ./")
    deps_copy_block = "\n".join(deps_copy_lines)
    if deps_copy_block:
        deps_copy_block += "\n"

    synthetic = clone_dir / "Dockerfile.chap-models-checker"
    synthetic.write_text(
        f"FROM {base}\n"
        "WORKDIR /app\n"
        f"{deps_copy_block}"
        "RUN if [ -f pyproject.toml ]; then uv sync --no-dev || true; fi\n"
        "COPY . .\n"
        "RUN if [ -f pyproject.toml ]; then uv sync --no-dev || true; fi\n"
        "ENV PORT=8000 HOST=0.0.0.0\n"
        f"{cmd}\n"
    )
    return synthetic


@contextmanager
def _uv_chapkit_service(
    clone_dir: Path, log_dir: Path | None, startup_timeout: float, skip_uv_sync: bool
) -> Generator[str, None, None]:
    if not skip_uv_sync:
        uv_sync(clone_dir)

    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    flat_main = clone_dir / "main.py"
    src_pkg = _detect_src_package(clone_dir)

    env = os.environ.copy()
    if src_pkg is not None:
        env["HOST"] = "127.0.0.1"
        env["PORT"] = str(port)
        cmd = ["uv", "run", "python", "-m", src_pkg]
    elif flat_main.exists():
        cmd = ["uv", "run", "fastapi", "dev", "--host", "127.0.0.1", "--port", str(port)]
    else:
        raise ChapkitServiceError(f"{clone_dir.name}: no main.py at root and no src/<pkg>/__main__.py")

    # Track the file separately from the subprocess stdout sink so cleanup
    # is unambiguous. `isinstance(handle, typing.IO)` evaluates False at
    # runtime (typing.IO is a Protocol), which previously left the file
    # leaked across the lifetime of the process pool.
    service_log: IO[bytes] | None = None
    stdout_target: IO[bytes] | int = subprocess.DEVNULL
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        service_log = (log_dir / f"{clone_dir.name}.service.log").open("wb")
        stdout_target = service_log

    try:
        # start_new_session=True puts uv + every child it spawns (uvicorn
        # worker, fastapi dev's reloader, …) into one process group so
        # kill_process_tree can reap them as a unit. Without it, only the
        # top-level uv process gets the SIGTERM and the actual server keeps
        # holding the port for the next repo.
        proc = subprocess.Popen(
            cmd,
            cwd=clone_dir,
            stdout=stdout_target,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        if service_log is not None:
            service_log.close()
        raise ChapkitServiceError(
            f"could not launch chapkit service for {clone_dir.name}: {cmd[0]} not on PATH"
        ) from exc

    try:
        _wait_healthy(url, proc, startup_timeout)
        yield url
    finally:
        kill_process_tree(proc)
        if service_log is not None:
            service_log.close()


@contextmanager
def _docker_chapkit_service(
    clone_dir: Path, log_dir: Path | None, startup_timeout: float, build_timeout: float
) -> Generator[str, None, None]:
    """Build the repo's Dockerfile and run it on a free host port (container port 8000)."""
    image_tag = f"chap-models-checker/{clone_dir.name.lower()}:latest"
    log_dir = log_dir or clone_dir.parent
    log_dir.mkdir(parents=True, exist_ok=True)
    build_log = log_dir / f"{clone_dir.name}.docker-build.log"
    run_log = log_dir / f"{clone_dir.name}.service.log"

    dockerfile = _ensure_dockerfile(clone_dir)
    # R-INLA images are amd64-only; always build with --platform=linux/amd64 so
    # we don't lose at the uv-sync step on Apple Silicon when the model author
    # forgot to pin the platform themselves.
    logger.info("docker build %s (%s, dockerfile=%s)", image_tag, clone_dir.name, dockerfile.name)
    with build_log.open("wb") as bl:
        try:
            build = subprocess.run(
                ["docker", "build", "--platform", "linux/amd64", "-f", dockerfile.name, "-t", image_tag, "."],
                cwd=clone_dir,
                stdout=bl,
                stderr=subprocess.STDOUT,
                timeout=build_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # Don't propagate TimeoutExpired — _process_chapkit only catches
            # ChapkitServiceError, so a long R-INLA build hitting the
            # build_timeout would otherwise abort the whole sweep.
            raise ChapkitServiceError(
                f"docker build timed out after {build_timeout:.0f}s for {clone_dir.name} (see {build_log})"
            ) from exc
        except FileNotFoundError as exc:
            # Same shape: missing docker CLI must not abort the sweep.
            raise ChapkitServiceError(f"docker CLI not on PATH (needed to build {clone_dir.name})") from exc
    if build.returncode != 0:
        raise ChapkitServiceError(f"docker build failed for {clone_dir.name} (see {build_log})")

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    logger.info("docker run %s on port %s", image_tag, port)
    try:
        run = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--platform",
                "linux/amd64",
                "-p",
                f"{port}:8000",
                "-e",
                "PORT=8000",
                "-e",
                "HOST=0.0.0.0",
                image_tag,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ChapkitServiceError(f"docker CLI not on PATH (needed to start {clone_dir.name})") from exc
    except subprocess.TimeoutExpired as exc:
        raise ChapkitServiceError(f"docker run timed out for {clone_dir.name}") from exc
    if run.returncode != 0:
        raise ChapkitServiceError(f"docker run failed: {run.stderr.strip()[:500]}")
    container_id = run.stdout.strip()

    # Tee container logs to disk in the background.
    log_handle = run_log.open("wb")
    log_proc = subprocess.Popen(
        ["docker", "logs", "-f", container_id],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        # Health probe (no Popen handle to inspect; use a shim that checks `docker inspect`).
        # Both the inspect and the cleanup `docker stop` get explicit timeouts
        # — without them a wedged daemon could stall the sweep past
        # startup_timeout. Subprocess timeout / FileNotFound translate into a
        # ChapkitServiceError so the per-repo handler picks the failure up.
        deadline = time.time() + startup_timeout
        with httpx.Client(timeout=2.0) as client:
            while time.time() < deadline:
                try:
                    inspect = subprocess.run(
                        ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise ChapkitServiceError(
                        f"docker inspect for {container_id[:12]} timed out (daemon stuck?)"
                    ) from exc
                except FileNotFoundError as exc:
                    raise ChapkitServiceError("docker CLI not on PATH during health probe") from exc
                if inspect.stdout.strip() != "true":
                    raise ChapkitServiceError(f"container exited during startup (see {run_log})")
                try:
                    if client.get(f"{url}/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                raise ChapkitServiceError(f"container at {url} did not become healthy in {startup_timeout:.0f}s")
        yield url
    finally:
        log_proc.terminate()
        try:
            log_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log_proc.kill()
            # Reap the killed process so we don't leave a zombie and so
            # the log file handle isn't closed while writes are still in
            # flight from `docker logs -f`.
            try:
                log_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        log_handle.close()
        try:
            subprocess.run(
                ["docker", "stop", "-t", "5", container_id],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # Best-effort cleanup; don't block the sweep if the daemon is
            # gone or unresponsive at teardown time.
            pass


@contextmanager
def chapkit_service(
    clone_dir: Path,
    *,
    log_dir: Path | None = None,
    startup_timeout: float = 120.0,
    skip_uv_sync: bool = False,
    docker_build_timeout: float = 1200.0,
    force_mode: str | None = None,
) -> Generator[str, None, None]:
    """Yield the base URL of a running chapkit service started from ``clone_dir``.

    Picks the right runtime: ``docker`` for R-backed models, ``uv`` for pure-Python.
    Override with ``force_mode="docker"`` or ``force_mode="uv"``.
    """
    mode = force_mode or ("docker" if needs_docker(clone_dir) else "uv")
    if mode == "docker":
        with _docker_chapkit_service(clone_dir, log_dir, startup_timeout, docker_build_timeout) as url:
            yield url
    else:
        with _uv_chapkit_service(clone_dir, log_dir, startup_timeout, skip_uv_sync) as url:
            yield url


def fetch_info(url: str, timeout: float = 15.0) -> tuple[dict[str, Any], dict[str, Any]]:
    """GET ``/api/v1/info`` and a defaults-merged config schema.

    The JSON schema returned by ``/api/v1/configs/$schema`` does *not* surface
    ``default_factory`` defaults (Pydantic omits them). We therefore POST a
    probe config with an empty body, read back the server-resolved defaults,
    and graft them onto the schema's ``properties.<key>.default`` so callers
    get the same shape they expect from a fully-defaulted JSONSchema.
    """
    with httpx.Client(timeout=timeout) as client:
        info_resp = client.get(f"{url}/api/v1/info")
        info_resp.raise_for_status()
        info = info_resp.json()
        schema_resp = client.get(f"{url}/api/v1/configs/$schema")
        schema: dict[str, Any] = schema_resp.json() if schema_resp.status_code == 200 else {}

        # Materialize default_factory values via a probe config.
        try:
            probe = client.post(
                f"{url}/api/v1/configs",
                json={"name": "_probe_defaults", "data": {}},
            )
            if probe.status_code in (200, 201):
                resolved = probe.json().get("data") or {}
                props = schema.setdefault("properties", {})
                for key, value in resolved.items():
                    field = props.setdefault(key, {})
                    if isinstance(field, dict) and "default" not in field:
                        field["default"] = value
                # Best-effort cleanup; ignore failures (some services may forbid delete).
                config_id = probe.json().get("id")
                if config_id:
                    client.delete(f"{url}/api/v1/configs/{config_id}")
        except httpx.HTTPError:
            pass

    return info, schema
