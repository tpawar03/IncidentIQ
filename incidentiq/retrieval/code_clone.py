"""Cache-aware shallow clone-at-commit over the git CLI (FR-26).

Standard "shallow clone one commit" recipe: `git init` + `git fetch --depth 1 origin
<commit>` + `git checkout FETCH_HEAD` — works for arbitrary historical SHAs (not just a
branch tip) against any remote that serves them, including local paths (our test fixture
repo and, later, the OTel demo mirror).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from incidentiq import config
from incidentiq.errors import llm_error


def _cache_key(repo_url: str, commit: str) -> str:
    # commit alone isn't unique across repos; hash (repo_url, commit) into one cache slot.
    return hashlib.sha256(f"{repo_url}@{commit}".encode()).hexdigest()[:16]


def _run(args: list[str], *, timeout: float) -> None:
    subprocess.run(args, check=True, capture_output=True, timeout=timeout, text=True)


def clone_at_commit(
    repo_url: str,
    commit: str,
    *,
    cache_root: Path | None = None,
    timeout: float | None = None,
) -> Path:
    """Return a local working tree of `repo_url` checked out at `commit`.

    Cache hit (dest already has this exact repo_url+commit cloned) is a no-op path lookup,
    <5s (FR-26). Cache miss shells out to git; any failure (bad commit, unreachable repo,
    timeout) raises LLMCallError(kind="clone_timeout") — the caller escalates.
    """
    root = cache_root if cache_root is not None else Path(config.AST_CLONE_CACHE_ROOT)
    bound = config.AST_CLONE_TIMEOUT_SECONDS if timeout is None else timeout
    dest = root / _cache_key(repo_url, commit)

    if (dest / ".git").exists():
        return dest                                       # cache hit

    root.mkdir(parents=True, exist_ok=True)
    try:
        _run(["git", "init", "-q", str(dest)], timeout=bound)
        _run(["git", "-C", str(dest), "remote", "add", "origin", repo_url], timeout=bound)
        _run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", commit], timeout=bound)
        _run(["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"], timeout=bound)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        shutil.rmtree(dest, ignore_errors=True)            # never cache a partial/broken clone
        raise llm_error(
            "clone_timeout",
            f"git clone/checkout failed for {repo_url}@{commit}: {e}",
            node="ast_code_retriever",
        ) from e
    return dest
