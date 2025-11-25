# Python
import subprocess
from pathlib import Path
from typing import Optional, Dict


def get_git_status(repo_path: Path) -> Dict[str, Optional[object]]:
    """
    Get the current Git short commit hash and whether the branch is up to date with its upstream.

    Returns a dict:
      {
        "short_hash": Optional[str],   # e.g., "a1b2c3d", None if not a git repo
        "branch": Optional[str],       # current branch name or "HEAD" if detached, None if not a repo
        "up_to_date": Optional[bool],  # True/False if upstream exists, None if unknown/no upstream
        "ahead": Optional[int],        # commits ahead of upstream (0 if up to date), None if unknown
        "behind": Optional[int],       # commits behind upstream (0 if up to date), None if unknown
      }
    """

    def run_git(args, check=False):
        return subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            check=check,
        )

    result: Dict[str, Optional[object]] = {
        "short_hash": None,
        "branch": None,
        "up_to_date": None,
        "ahead": None,
        "behind": None,
    }

    # Verify we're in a git repository
    try:
        inside = run_git(["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return result  # Not a repository
    except Exception:
        return result

    # Get short hash
    try:
        hash_res = run_git(["rev-parse", "--short", "HEAD"], check=True)
        result["short_hash"] = hash_res.stdout.strip()
    except subprocess.CalledProcessError:
        # Detached or other state where HEAD isn't resolvable
        result["short_hash"] = None

    # Get branch (or "HEAD" if detached)
    try:
        branch_res = run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=True)
        result["branch"] = branch_res.stdout.strip()
    except subprocess.CalledProcessError:
        result["branch"] = None

    # Find upstream; if none, we can't determine up-to-date status against a remote
    try:
        upstream_res = run_git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
        )
        if upstream_res.returncode != 0:
            # No upstream configured
            return result

        upstream_ref = upstream_res.stdout.strip()

        # Ensure we compare against the latest remote state
        run_git(["fetch", "--quiet"])

        # Compute ahead/behind relative to upstream
        # Format: "<behind> <ahead>" because left is upstream, right is HEAD
        counts_res = run_git(
            ["rev-list", "--left-right", "--count", f"{upstream_ref}...HEAD"],
            check=True,
        )
        behind_str, ahead_str = counts_res.stdout.strip().split()
        behind, ahead = int(behind_str), int(ahead_str)

        result["behind"] = behind
        result["ahead"] = ahead
        result["up_to_date"] = behind == 0 and ahead == 0
    except subprocess.CalledProcessError:
        # If comparison fails, leave up_to_date/ahead/behind as None
        pass
    except ValueError:
        # Unexpected parsing format
        pass

    return result
