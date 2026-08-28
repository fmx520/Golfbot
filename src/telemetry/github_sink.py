"""Durable race-log storage backed by the repo itself.

The repo is already durable, already free, and already something we have
credentials for, which makes it a reasonable home for ~10 small records a week
without standing up another service.

Two design points that are not obvious:

**One file per race, never an appended JSONL.** Appending would mean read the
file, modify, write it back — three problems at once: a GET on every race, a
1MB ceiling on the contents API, and a lost-update race if two workers ever
finish together. A create-only write per race has none of those.

**Commits land on a side branch, not the default one.** Render redeploys on
pushes to the branch it watches. A bot that commits its own telemetry to that
branch would restart the service after every race — and, if a race ran near a
release window, potentially kill the next one. ``RACE_LOG_GITHUB_BRANCH``
defaults to ``race-logs`` and is created on first use.

Everything here is best effort. A telemetry failure must never affect a
booking, so all paths swallow their exceptions and report status instead.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

GITHUB_API = "https://api.github.com"

# The race is already decided when this runs, but the worker should not hang on
# a slow API during teardown.
TIMEOUT_SECONDS = 6.0

# Whether the log branch exists is process-wide state; checking it once per
# worker rather than once per race saves a round trip on every race but the
# first.
_branch_ready: set[str] = set()


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def _log_error(detail: str) -> None:
    try:
        sys.stdout.write(f"GOLFBOT_RACE_SINK_ERROR github: {detail}\n")
        sys.stdout.flush()
    except Exception:
        pass


def _ensure_branch(client: Any, repo: str, branch: str, token: str) -> Optional[str]:
    """Create the log branch off the default branch if it is missing.

    Returns None on success, or an error string. Being missing is the expected
    state on first run, so it is not treated as a failure.
    """
    key = f"{repo}@{branch}"
    if key in _branch_ready:
        return None

    ref = client.get(
        f"{GITHUB_API}/repos/{repo}/git/ref/heads/{branch}",
        headers=_headers(token),
        timeout=TIMEOUT_SECONDS,
    )
    if ref.status_code == 200:
        _branch_ready.add(key)
        return None
    if ref.status_code != 404:
        return f"could not read branch {branch}: {ref.status_code} {ref.text[:200]}"

    # Branch does not exist yet — point a new one at the default branch's head.
    meta = client.get(
        f"{GITHUB_API}/repos/{repo}",
        headers=_headers(token),
        timeout=TIMEOUT_SECONDS,
    )
    if meta.status_code >= 400:
        return f"could not read repo: {meta.status_code} {meta.text[:200]}"
    default_branch = (meta.json() or {}).get("default_branch", "main")

    head = client.get(
        f"{GITHUB_API}/repos/{repo}/git/ref/heads/{default_branch}",
        headers=_headers(token),
        timeout=TIMEOUT_SECONDS,
    )
    if head.status_code >= 400:
        return f"could not read {default_branch}: {head.status_code} {head.text[:200]}"
    sha = ((head.json() or {}).get("object") or {}).get("sha")
    if not sha:
        return f"no sha on {default_branch}"

    created = client.post(
        f"{GITHUB_API}/repos/{repo}/git/refs",
        headers=_headers(token),
        json={"ref": f"refs/heads/{branch}", "sha": sha},
        timeout=TIMEOUT_SECONDS,
    )
    # 422 means someone else created it between our check and our create.
    if created.status_code >= 400 and created.status_code != 422:
        return f"could not create branch {branch}: {created.status_code} {created.text[:200]}"

    _branch_ready.add(key)
    return None


def post_race(record: dict[str, Any]) -> dict[str, Any]:
    """Write one race record to the repo. Never raises."""
    repo = os.getenv("RACE_LOG_GITHUB_REPO", "").strip()
    token = os.getenv("RACE_LOG_GITHUB_TOKEN", "").strip()
    if not repo or not token:
        return {"configured": False}

    branch = os.getenv("RACE_LOG_GITHUB_BRANCH", "race-logs").strip() or "race-logs"

    try:
        import httpx  # already a dependency; imported late to keep startup lean

        with httpx.Client() as client:
            err = _ensure_branch(client, repo, branch, token)
            if err:
                _log_error(err)
                return {"configured": True, "ok": False, "error": err}

            # Sorts chronologically as plain text, and the date prefixes keep
            # any single directory from growing without bound.
            now = datetime.now(timezone.utc)
            stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
            run_id = record.get("run_id", "unknown")
            path = f"race-logs/{now:%Y/%m}/{stamp}-{run_id}.json"

            body = json.dumps(record, indent=2, default=str)
            outcome = record.get("outcome", "unknown")
            response = client.put(
                f"{GITHUB_API}/repos/{repo}/contents/{path}",
                headers=_headers(token),
                json={
                    "message": f"race {run_id}: {outcome}",
                    "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
                    "branch": branch,
                },
                timeout=TIMEOUT_SECONDS,
            )
            ok = response.status_code < 400
            if not ok:
                _log_error(f"{response.status_code} {response.text[:200]}")
                return {
                    "configured": True,
                    "ok": False,
                    "status_code": response.status_code,
                    "body": response.text[:200],
                }
            return {"configured": True, "ok": True, "path": path, "branch": branch}

    except Exception as exc:  # noqa: BLE001 — telemetry must never kill a run
        _log_error(f"{type(exc).__name__}: {exc}")
        return {"configured": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
