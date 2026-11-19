#!/usr/bin/env python3
"""Read-only Git verification for a candidate containing public snapshot data."""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

_SPEC = importlib.util.spec_from_file_location(
    "public_snapshot_validator", Path(__file__).with_name("update_public_snapshot.py"))
_snapshot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_snapshot)

SNAPSHOT_PATHS = ("data/latest.json", "data/manifest.json")
_ALLOWED = frozenset(path.encode("ascii") for path in SNAPSHOT_PATHS)


class GuardError(Exception):
    """A deliberately redacted verification error."""


def _require(condition):
    if not condition:
        raise GuardError("Snapshot Git verification failed.")


def _sha(value, length=40):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{%d}" % length, value))
    return value


def _git(repo, *arguments):
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repo), *arguments], capture_output=True,
            check=False, timeout=20)
    except (OSError, subprocess.SubprocessError):
        raise GuardError("Snapshot Git verification failed.") from None
    _require(result.returncode == 0)
    return result.stdout


def verify_worktree(repo, expected_head_sha):
    """Reject a moved HEAD or any visible changes outside the snapshot pair."""
    _sha(expected_head_sha)
    _require(_git(repo, "rev-parse", "--verify", "HEAD").strip() == expected_head_sha.encode("ascii"))
    status = _git(repo, "-c", "status.renames=false", "status", "--porcelain=v1", "-z",
                  "--untracked-files=all", "--ignore-submodules=none")
    for entry in status.split(b"\0"):
        if not entry:
            continue
        _require(len(entry) >= 4 and entry[2:3] == b" ")
        _require(entry[3:] in _ALLOWED)
        _require(entry[:2] == b"??" or all(char in b" AM" for char in entry[:2]))


def _read_pair(repo, head_sha):
    entries = _git(repo, "ls-tree", "-z", head_sha, "--", *SNAPSHOT_PATHS)
    blobs = {}
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, path = entry.split(b"\t", 1)
            mode, kind, object_sha = metadata.split(b" ")
        except ValueError:
            raise GuardError("Snapshot Git verification failed.") from None
        _require(path in _ALLOWED and path not in blobs)
        _require(mode == b"100644" and kind == b"blob")
        _sha(object_sha.decode("ascii"))
        size = _git(repo, "cat-file", "-s", object_sha.decode("ascii")).strip()
        _require(size.isdigit())
        limit = _snapshot.MAX_BYTES if path == b"data/latest.json" else 64_000
        _require(int(size) <= limit)
        content = _git(repo, "cat-file", "blob", object_sha.decode("ascii"))
        _require(len(content) == int(size))
        blobs[path] = content
    _require(set(blobs) == _ALLOWED)
    return blobs[b"data/latest.json"], blobs[b"data/manifest.json"]


def _verify_pair(feed_bytes, manifest_bytes, current_source_sha256):
    try:
        feed = json.loads(feed_bytes.decode("utf-8"), object_pairs_hook=_snapshot.pairs)
        validated = _snapshot.validate(feed)
        _require(feed_bytes == _snapshot.encoded(validated))
        manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=_snapshot.pairs)
        _require(type(manifest) is dict)
        capture = _snapshot.timestamp(manifest["captured_at_utc"])
        _require(capture <= dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        _require(type(manifest["item_count"]) is int)
        digest = hashlib.sha256(feed_bytes).hexdigest()
        _require(digest == current_source_sha256)
        # Reconstruct metadata using the reviewed exporter, without rewriting
        # candidate data or inferring publication eligibility from new fields.
        with tempfile.TemporaryDirectory(prefix="public-snapshot-verify-") as temporary:
            folder = Path(temporary)
            source = folder / "source.json"
            source.write_bytes(feed_bytes)
            _snapshot.update(folder / "expected", source)
            expected = json.loads((folder / "expected/manifest.json").read_bytes())
        expected["captured_at_utc"] = capture
        _require(manifest == expected)
    except GuardError:
        raise
    except Exception:
        raise GuardError("Snapshot Git verification failed.") from None
    return {"content_sha256": digest, "item_count": len(validated["items"]),
            "captured_at_utc": capture}


def verify_snapshot_candidate(repo, expected_base_sha, expected_head_sha, current_source_sha256):
    """Validate immutable Git objects against a pinned base and public source hash.

    A changed candidate must be one nonempty commit directly on the supplied
    base. A no-op uses the same base and head. This function does not fetch,
    stage, commit, check out, push, or modify the repository.
    """
    _sha(expected_base_sha)
    _sha(expected_head_sha)
    _sha(current_source_sha256, 64)
    for commit in {expected_base_sha, expected_head_sha}:
        _require(_git(repo, "cat-file", "-t", commit).strip() == b"commit")
    changed = expected_head_sha != expected_base_sha
    if changed:
        headers = _git(repo, "cat-file", "commit", expected_head_sha).split(b"\n\n", 1)[0]
        parents = [line[7:] for line in headers.splitlines() if line.startswith(b"parent ")]
        _require(parents == [expected_base_sha.encode("ascii")])
        paths = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "--no-renames",
                     "-r", "-z", expected_base_sha, expected_head_sha, "--").split(b"\0")
        changed_paths = {path for path in paths if path}
        _require(bool(changed_paths) and changed_paths <= _ALLOWED)
    feed_bytes, manifest_bytes = _read_pair(repo, expected_head_sha)
    metadata = _verify_pair(feed_bytes, manifest_bytes, current_source_sha256)
    return {"base_sha": expected_base_sha, "head_sha": expected_head_sha,
            "changed": changed, **metadata}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True)
    parser.add_argument("--head")
    parser.add_argument("--source-sha256")
    parser.add_argument("--check-worktree", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.check_worktree:
            _require(arguments.head is None and arguments.source_sha256 is None)
            verify_worktree(arguments.repo, arguments.base)
            result = {"worktree_verified": True, "base_sha": arguments.base}
        else:
            result = verify_snapshot_candidate(arguments.repo, arguments.base,
                                               arguments.head, arguments.source_sha256)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print("Snapshot Git verification failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
