#!/usr/bin/env python3
"""Propose a validated public snapshot without pushing or merging main."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import snapshot_git_guard as guard
import update_public_snapshot as snapshot

REPOSITORY = "rasesolutions/ai-intelligence-public"
FILES = ("data/latest.json", "data/manifest.json")
SHA = re.compile(r"[0-9a-f]{40}")
BRANCH = re.compile(r"public-feed(?:-probe)?/[0-9a-f]{12}-[0-9a-f]{12}")


class PublicationError(Exception):
    """Safe failure that never contains process output or feed content."""


class PendingPublication(PublicationError):
    def __init__(self, result):
        self.result = result


def require(value):
    if not value:
        raise PublicationError("Snapshot proposal validation failed.")


def process(arguments, repo, input_value=None):
    try:
        return subprocess.run(arguments, cwd=repo, input=input_value, text=True,
                              encoding="utf-8", capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.SubprocessError):
        raise PublicationError("Snapshot proposal command failed.") from None


def git(repo, *arguments):
    result = process(["git", *arguments], repo)
    require(result.returncode == 0)
    return result.stdout.strip()


def api(repo, path, method="GET", payload=None, missing=False):
    arguments = ["gh", "api", "--hostname", "github.com", "--method", method,
                 "-H", "Accept: application/vnd.github+json", path]
    value = None
    if payload is not None:
        arguments.extend(["--input", "-"])
        value = json.dumps(payload)
    result = process(arguments, repo, value)
    if result.returncode:
        if missing and re.search(r"\bHTTP 404\b", result.stderr):
            return None
        raise PublicationError("GitHub request failed.")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise PublicationError("GitHub response validation failed.") from None


def valid_sha(value):
    require(type(value) is str and SHA.fullmatch(value))
    return value


def pair(repo):
    result = []
    for name in FILES:
        path = repo / name
        require(path.is_file() and not path.is_symlink())
        with path.open("rb") as stream:
            content = stream.read(snapshot.MAX_BYTES + 1)
        require(len(content) <= snapshot.MAX_BYTES)
        result.append(content)
    return tuple(result)


def validate_pair(contents):
    latest, manifest_bytes = contents
    current = snapshot.validate(json.loads(latest, object_pairs_hook=snapshot.pairs))
    require(snapshot.encoded(current) == latest)
    manifest = json.loads(manifest_bytes, object_pairs_hook=snapshot.pairs)
    capture = snapshot.timestamp(manifest["captured_at_utc"])
    require(capture <= dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    require(type(manifest["item_count"]) is int)
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "input.json"
        source.write_bytes(latest)
        output = Path(folder) / "verified"
        snapshot.update(output, source)
        expected = json.loads((output / "manifest.json").read_bytes())
    expected["captured_at_utc"] = capture
    require(manifest == expected)
    raw = snapshot.fetch()
    require(len(raw) <= snapshot.MAX_BYTES)
    fresh = snapshot.encoded(snapshot.validate(json.loads(raw, object_pairs_hook=snapshot.pairs)))
    require(fresh == latest)
    return hashlib.sha256(latest).hexdigest()


def branch_name(base_sha, contents, probe=False):
    digest = hashlib.sha256()
    for name, content in zip(FILES, contents):
        digest.update(name.encode("ascii") + b"\0" + str(len(content)).encode("ascii") + b"\0" + content)
    prefix = "public-feed-probe" if probe else "public-feed"
    return f"{prefix}/{base_sha[:12]}-{digest.hexdigest()[:12]}"


def identity(pr, state, base_sha):
    require(type(pr["number"]) is int and pr["number"] > 0)
    head = valid_sha(pr["head"]["sha"])
    return {"state": state, "pr_number": pr["number"],
            "pr_url": f"https://github.com/{REPOSITORY}/pull/{pr['number']}",
            "base_sha": base_sha, "head_sha": head}


def open_proposals(repo):
    proposals = []
    for page in range(1, 4):
        values = api(repo, f"repos/{REPOSITORY}/pulls?state=open&base=main&per_page=100&page={page}")
        require(type(values) is list)
        for value in values:
            if type(value) is not dict:
                continue
            user, head, base = (value.get(name) for name in ("user", "head", "base"))
            if not all(type(part) is dict for part in (user, head, base)):
                continue
            head_repo, base_repo = head.get("repo"), base.get("repo")
            ref = head.get("ref")
            if (user.get("login") == "github-actions[bot]" and user.get("type") == "Bot"
                    and type(head_repo) is dict and head_repo.get("full_name") == REPOSITORY
                    and type(base_repo) is dict and base_repo.get("full_name") == REPOSITORY
                    and base.get("ref") == "main" and type(ref) is str and BRANCH.fullmatch(ref)):
                proposals.append(value)
        if len(values) < 100:
            return proposals
    raise PublicationError("Too many open pull requests to validate safely.")


def remote_pair(repo, head_sha):
    contents = []
    for name in FILES:
        value = api(repo, f"repos/{REPOSITORY}/contents/{name}?ref={head_sha}")
        require(value["encoding"] == "base64" and value["path"] == name)
        content = base64.b64decode(value["content"], validate=False)
        require(len(content) <= snapshot.MAX_BYTES)
        blob = hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()
        require(value["sha"] == blob)
        contents.append(content)
    return tuple(contents)


def fetch_candidate(repo, branch, base_sha, head_sha, source_sha):
    require(BRANCH.fullmatch(branch))
    git(repo, "fetch", "--no-tags", "--depth=2", "origin", f"refs/heads/{branch}")
    require(git(repo, "rev-parse", "FETCH_HEAD") == head_sha)
    return guard.verify_snapshot_candidate(repo, base_sha, head_sha, source_sha)


def validate_pr(repo, pr, branch, base_sha, contents, source_sha):
    require(pr["state"] == "open" and pr["user"]["login"] == "github-actions[bot]"
            and pr["user"]["type"] == "Bot")
    require(pr["base"]["ref"] == "main" and pr["base"]["repo"]["full_name"] == REPOSITORY)
    require(pr["base"]["sha"] == base_sha)
    require(pr["head"]["ref"] == branch and pr["head"]["repo"]["full_name"] == REPOSITORY)
    head_sha = valid_sha(pr["head"]["sha"])
    ref = api(repo, f"repos/{REPOSITORY}/git/ref/heads/{branch}")
    require(ref["object"]["type"] == "commit" and ref["object"]["sha"] == head_sha)
    require(remote_pair(repo, head_sha) == contents)
    fetch_candidate(repo, branch, base_sha, head_sha, source_sha)
    return head_sha


def propose(repo, base_sha, probe=False):
    repo = Path(repo).resolve()
    valid_sha(base_sha)
    require(bool(os.environ.get("GH_TOKEN")))
    expected_remote = f"https://github.com/{REPOSITORY}.git"
    require(git(repo, "remote", "get-url", "origin") == expected_remote)
    require(git(repo, "remote", "get-url", "--push", "--all", "origin") == expected_remote)
    guard.verify_worktree(repo, base_sha)
    contents = pair(repo)
    source_sha = validate_pair(contents)
    main = api(repo, f"repos/{REPOSITORY}/git/ref/heads/main")
    require(main["object"]["type"] == "commit" and main["object"]["sha"] == base_sha)
    if not git(repo, "diff", "--name-only", "HEAD", "--", *FILES):
        return {"state": "noop", "base_sha": base_sha, "head_sha": base_sha}
    branch = branch_name(base_sha, contents, probe)
    existing = open_proposals(repo)
    if existing:
        if len(existing) == 1 and existing[0]["head"]["ref"] == branch:
            validate_pr(repo, existing[0], branch, base_sha, contents, source_sha)
            return identity(existing[0], "verification_reused" if probe else "reused", base_sha)
        raise PendingPublication(identity(existing[0], "pending", base_sha))
    ref = api(repo, f"repos/{REPOSITORY}/git/ref/heads/{branch}", missing=True)
    if ref is None:
        git(repo, "switch", "-c", branch)
        git(repo, "add", "--", *FILES)
        guard.verify_worktree(repo, base_sha)
        git(repo, "-c", "user.name=Rase public feed snapshot", "-c",
            "user.email=snapshot@users.noreply.github.com", "commit", "-m",
            "Propose approved public AI developments")
        head_sha = valid_sha(git(repo, "rev-parse", "HEAD"))
        guard.verify_snapshot_candidate(repo, base_sha, head_sha, source_sha)
        git(repo, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
            "push", "origin", f"{head_sha}:refs/heads/{branch}")
    else:
        require(ref["object"]["type"] == "commit")
        head_sha = valid_sha(ref["object"]["sha"])
        require(remote_pair(repo, head_sha) == contents)
        fetch_candidate(repo, branch, base_sha, head_sha, source_sha)
    # Recheck the remote base immediately before proposing. A race fails closed.
    main = api(repo, f"repos/{REPOSITORY}/git/ref/heads/main")
    require(main["object"]["sha"] == base_sha)
    old = api(repo, f"repos/{REPOSITORY}/pulls?state=all&head=rasesolutions:{branch}&per_page=100")
    require(type(old) is list and not old)
    title = "Verification only: public snapshot refresh" if probe else "Refresh approved public AI developments"
    body = ("Automated proposal from the approved public HTTPS feed. Only the two public snapshot files may change. "
            f"Base commit: {base_sha}. Candidate commit: {head_sha}. "
            "CI and the normal merge controls must pass; this workflow cannot merge or push main.")
    if probe:
        body += " Verification-only proposal: close without merging after CI verification."
    pr = api(repo, f"repos/{REPOSITORY}/pulls", "POST", {
        "title": title, "body": body, "head": branch, "base": "main", "draft": False,
        "maintainer_can_modify": False})
    validate_pr(repo, pr, branch, base_sha, contents, source_sha)
    return identity(pr, "verification_created" if probe else "created", base_sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--verification-probe", action="store_true")
    arguments = parser.parse_args()
    try:
        print(json.dumps(propose(arguments.repo, arguments.base_sha, arguments.verification_probe), sort_keys=True))
        return 0
    except PendingPublication as error:
        print(json.dumps(error.result, sort_keys=True))
        return 1
    except Exception:
        print("Snapshot proposal failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
