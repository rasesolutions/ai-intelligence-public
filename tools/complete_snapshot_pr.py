#!/usr/bin/env python3
"""Finish a verified data-only PR using the publisher's existing user authentication."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import update_public_snapshot as snapshot
import snapshot_git_guard as guard

REPOSITORY = "rasesolutions/ai-intelligence-public"
PUBLISHER = "raakesh-blokhra"
PREFIX = "repos/" + REPOSITORY + "/"
WORKFLOW = ".github/workflows/validate-public-snapshot.yml"
SHA = re.compile(r"[0-9a-f]{40}")
NORMAL_BRANCH = re.compile(r"public-feed/[0-9a-f]{12}-[0-9a-f]{12}")
PROBE_BRANCH = re.compile(r"public-feed-probe/[0-9a-f]{12}-[0-9a-f]{12}")
ALLOWED = {"data/latest.json", "data/manifest.json"}


class CompletionError(Exception):
    """Safe diagnostic: never include command output or feed text."""


def require(condition, message="Snapshot PR verification failed."):
    if not condition:
        raise CompletionError(message)


def command(arguments, cwd=None, payload=None):
    try:
        result = subprocess.run(arguments, cwd=cwd, input=payload, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=60, check=False, encoding="utf-8")
    except (OSError, subprocess.SubprocessError):
        raise CompletionError("Snapshot command failed.") from None
    require(result.returncode == 0, "Snapshot command failed.")
    return result.stdout


def api(route, method="GET", payload=None):
    arguments = ["gh", "api", "--hostname", "github.com", PREFIX + route, "--method", method]
    body = None
    if payload is not None:
        arguments += ["--input", "-"]
        body = json.dumps(payload)
    raw = command(arguments, payload=body)
    try:
        return json.loads(raw) if raw.strip() else None
    except (ValueError, TypeError):
        raise CompletionError("Unexpected GitHub response.") from None


def source_hash():
    try:
        raw = snapshot.fetch()
        snapshot.require(len(raw) <= snapshot.MAX_BYTES)
        public = snapshot.validate(json.loads(raw.decode("utf-8"), object_pairs_hook=snapshot.pairs))
        return hashlib.sha256(snapshot.encoded(public)).hexdigest()
    except Exception:
        raise CompletionError("Live public feed verification failed.") from None


def verify_protection():
    protection = api("branches/main/protection")
    reviews = protection.get("required_pull_request_reviews", {})
    require(protection.get("enforce_admins", {}).get("enabled") is True)
    require(reviews.get("require_code_owner_reviews") is True)
    require(reviews.get("required_approving_review_count") == 0)
    require(not reviews.get("require_last_push_approval", False))
    require(not any(reviews.get("bypass_pull_request_allowances", {}).values()))
    checks = protection.get("required_status_checks", {})
    require(checks.get("strict") is True)
    require({"context": "validate", "app_id": 15368} in checks.get("checks", []))
    require(protection.get("allow_force_pushes", {}).get("enabled") is False)
    require(protection.get("allow_deletions", {}).get("enabled") is False)
    restrictions = protection.get("restrictions", {})
    require({user.get("login") for user in restrictions.get("users", [])} == {PUBLISHER})
    require(not restrictions.get("teams") and not restrictions.get("apps"))


def verify_pr(pr, expected_head, verify_only):
    require(pr.get("state") == "open" and pr.get("draft") is False)
    require(pr.get("user", {}).get("login") == "github-actions[bot]")
    require(pr.get("user", {}).get("type") == "Bot")
    require(pr.get("base", {}).get("ref") == "main")
    require(pr.get("base", {}).get("repo", {}).get("full_name") == REPOSITORY)
    head = pr.get("head", {})
    require(head.get("repo", {}).get("full_name") == REPOSITORY)
    require(head.get("sha") == expected_head and SHA.fullmatch(expected_head))
    branch = head.get("ref", "")
    probe = bool(PROBE_BRANCH.fullmatch(branch))
    require(bool(NORMAL_BRANCH.fullmatch(branch)) or (verify_only and probe))
    require(verify_only or not probe, "Verification probes cannot be merged.")
    require(pr.get("commits") == 1 and 0 < pr.get("changed_files", 0) <= 2)
    return probe


def verify_files(number):
    files = api(f"pulls/{number}/files?per_page=3")
    require(type(files) is list and 0 < len(files) <= 2)
    require(len({entry.get("filename") for entry in files}) == len(files))
    for entry in files:
        require(entry.get("filename") in ALLOWED and entry.get("status") == "modified")
        require("previous_filename" not in entry)


def verify_candidate(repo, number, expected_head, verify_only):
    pr = api(f"pulls/{number}")
    probe = verify_pr(pr, expected_head, verify_only)
    verify_files(number)
    base = api("git/ref/heads/main")["object"]["sha"]
    require(SHA.fullmatch(base) and pr["base"]["sha"] == base,
            "Snapshot base changed; regenerate from current main.")
    require(pr["head"]["ref"].split("/")[1].startswith(base[:12] + "-"))
    remote = command(["git", "config", "--get", "remote.origin.url"], cwd=repo).strip()
    require(remote == "https://github.com/" + REPOSITORY + ".git")
    command(["git", "fetch", "--no-tags", "origin", base, expected_head], cwd=repo)
    try:
        guard.verify_snapshot_candidate(repo, base, expected_head, source_hash())
    except Exception:
        raise CompletionError("Committed snapshot verification failed.") from None
    return pr, base, probe


def eligible_run(run, number, expected_head, workflow_id):
    return (run.get("event") == "pull_request" and run.get("head_sha") == expected_head
            and run.get("workflow_id") == workflow_id
            and run.get("path", "").split("@", 1)[0] == WORKFLOW
            and any(pr.get("number") == number for pr in run.get("pull_requests", [])))


def wait_for_ci(number, expected_head, approve_ci, wait_seconds):
    workflow_id = api("actions/workflows/validate-public-snapshot.yml")["id"]
    deadline = time.monotonic() + wait_seconds
    approved = set()
    while True:
        response = api("actions/workflows/validate-public-snapshot.yml/runs"
                       f"?event=pull_request&head_sha={expected_head}&per_page=100")
        matches = [run for run in response.get("workflow_runs", [])
                   if eligible_run(run, number, expected_head, workflow_id)]
        if matches:
            run = max(matches, key=lambda entry: entry["id"])
            run_id = run["id"]
            require(type(run_id) is int)
            if run.get("status") == "completed" and run.get("conclusion") == "success":
                jobs = api(f"actions/runs/{run_id}/jobs?per_page=100").get("jobs", [])
                require(any(job.get("name") == "validate" and job.get("conclusion") == "success"
                            for job in jobs), "Required validation job did not pass.")
                return run_id
            needs_approval = (run.get("conclusion") == "action_required"
                              or run.get("status") in ("waiting", "action_required"))
            if needs_approval:
                if run_id not in approved:
                    require(approve_ci, "Exact PR workflow execution requires approval.")
                    api(f"actions/runs/{run_id}/approve", "POST")
                    approved.add(run_id)
            elif run.get("status") == "completed":
                raise CompletionError("Required PR validation did not pass.")
        require(time.monotonic() < deadline, "Matching PR validation is not complete.")
        time.sleep(5)


def complete(repo, number, expected_head, approve_ci=False, merge=False, wait_seconds=300):
    require(type(number) is int and number > 0 and bool(SHA.fullmatch(expected_head)))
    # Never use the hosted bot token to approve execution or merge.
    user = json.loads(command(["gh", "api", "--hostname", "github.com", "user"]))
    require(user.get("type") == "User" and user.get("login") == PUBLISHER,
            "Existing publisher user authentication is required.")
    verify_protection()
    pr, base, probe = verify_candidate(repo, number, expected_head, not merge)
    run_id = wait_for_ci(number, expected_head, approve_ci, wait_seconds)
    # Re-read source, exact PR, base and committed bytes after CI, before merge.
    current, current_base, current_probe = verify_candidate(repo, number, expected_head, not merge)
    require(base == current_base and probe == current_probe)
    require(current.get("mergeable") is True, "GitHub has not confirmed merge eligibility.")
    result = {"state": "verified", "pr_number": number, "head_sha": expected_head,
              "base_sha": base, "ci_run_id": run_id, "verification_probe": probe}
    if merge:
        verify_protection()
        response = api(f"pulls/{number}/merge", "PUT",
                       {"sha": expected_head, "merge_method": "merge",
                        "commit_title": "Refresh approved public AI developments"})
        require(response.get("merged") is True, "GitHub did not merge the snapshot PR.")
        require(bool(SHA.fullmatch(response.get("sha", ""))))
        result.update(state="merged", merge_sha=response["sha"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--approve-ci", action="store_true")
    parser.add_argument("--merge", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=300, choices=range(1, 601))
    args = parser.parse_args()
    try:
        result = complete(args.repo, args.pr, args.expected_head, args.approve_ci,
                          args.merge, args.wait_seconds)
        print(json.dumps(result, sort_keys=True))
        return 0
    except CompletionError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Snapshot PR completion failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
