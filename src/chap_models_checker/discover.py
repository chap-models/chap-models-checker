"""List repos under the chap-models GitHub org."""

from __future__ import annotations

import httpx

from chap_models_checker.models import RepoInfo

GITHUB_API = "https://api.github.com"

# Repos that live under chap-models/ but aren't chap models themselves and so
# should not be exercised by the sweep. The checker itself goes here once
# pushed to the org - otherwise discovery would feed it back into chap eval
# and report the tooling repo as a "model".
SKIPPED_REPOS: frozenset[str] = frozenset({"chap-models-checker"})


def list_chap_models_repos(
    org: str = "chap-models",
    *,
    include_archived: bool = False,
    skip_dot_repos: bool = True,
    skipped_repos: frozenset[str] = SKIPPED_REPOS,
    timeout: float = 30.0,
) -> list[RepoInfo]:
    """Fetch every repo under `org` (paginated, public, unauthenticated)."""
    repos: list[RepoInfo] = []
    page = 1
    with httpx.Client(timeout=timeout, headers={"Accept": "application/vnd.github+json"}) as client:
        while True:
            resp = client.get(
                f"{GITHUB_API}/orgs/{org}/repos",
                params={"per_page": 100, "page": page, "type": "public"},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for r in batch:
                name = r["name"]
                if skip_dot_repos and name.startswith("."):
                    continue
                if name in skipped_repos:
                    continue
                if r.get("archived") and not include_archived:
                    continue
                repos.append(
                    RepoInfo(
                        name=name,
                        full_name=r["full_name"],
                        default_branch=r.get("default_branch") or "main",
                        archived=bool(r.get("archived", False)),
                        description=r.get("description"),
                        html_url=r["html_url"],
                        clone_url=r["clone_url"],
                    )
                )
            if len(batch) < 100:
                break
            page += 1

    repos.sort(key=lambda r: r.name.lower())
    return repos
