"""Integration tests using isolated, synthetic local Git repositories."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "snapshot_git_guard", Path(__file__).parents[1] / "tools/snapshot_git_guard.py")
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)


def public_feed():
    return {
        "version": "https://jsonfeed.org/version/1.1", "title": "Rase public feed",
        "home_page_url": "https://rasesolutions.com/ai/latest",
        "feed_url": "https://rasesolutions.com/ai/feed.json", "language": "en",
        "description": "Verified public developments.",
        "authors": [{"name": "Rase AI Intelligence", "url": "https://rasesolutions.com/ai"}],
        "items": [{"id": "urn:rase:ai:record:fixture", "url": "https://rasesolutions.com/ai/research/fixture",
                   "title": "Verified research", "summary": "Verified summary alpha.",
                   "content_text": "Verified public findings.", "date_published": "2026-11-17T13:01:02Z",
                   "tags": ["Research"], "external_url": "https://publisher.example/research",
                   "_rase": {"event_type": "research_publication", "original_event_date":
                             {"value": "2026-11-16", "precision": "day", "certainty": "verified"},
                             "website_publication_scope": "profile", "website_update_scope": "reviewed_event_publication",
                             "summary_scope": "event", "sources": [{"url": "https://publisher.example/research",
                                                                     "title": "Public paper", "kind": "primary"}]}}]}


class SnapshotGitGuard(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="snapshot-git-test-")
        self.folder = Path(self.temporary.name)
        self.repo = self.folder / "repo"
        self.repo.mkdir()
        self.git("init", "--object-format=sha1", "--template=", "-b", "main")
        self.git("config", "user.name", "Synthetic snapshot fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "commit.gpgsign", "false")
        hooks = self.folder / "empty-hooks"
        hooks.mkdir()
        self.git("config", "core.hooksPath", str(hooks))
        (self.repo / "README.md").write_text("Synthetic fixture baseline.\n")
        self.value = public_feed()
        self.write_pair()
        self.git("add", "--", "README.md", *guard.SNAPSHOT_PATHS)
        self.git("commit", "-m", "Synthetic baseline")
        self.base = self.head()

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments, input_bytes=None):
        result = subprocess.run(["git", "-C", str(self.repo), *arguments], input=input_bytes,
                                capture_output=True, timeout=20)
        if result.returncode:
            raise AssertionError("Synthetic Git fixture setup failed.")
        return result.stdout

    def head(self):
        return self.git("rev-parse", "HEAD").decode().strip()

    def write_pair(self):
        source = self.folder / "source.json"
        source.write_text(json.dumps(self.value), encoding="utf-8")
        guard._snapshot.update(self.repo / "data", source)

    def changed_pair(self):
        self.value["items"][0]["summary"] = "Verified summary beta."
        self.write_pair()

    def commit(self, *paths):
        self.git("add", "--", *(paths or guard.SNAPSHOT_PATHS))
        self.git("commit", "-m", "Synthetic snapshot candidate")
        return self.head()

    def digest(self):
        return hashlib.sha256((self.repo / "data/latest.json").read_bytes()).hexdigest()

    def verify(self, head=None, source_hash=None):
        return guard.verify_snapshot_candidate(self.repo, self.base, head or self.head(), source_hash or self.digest())

    def test_valid_update_uses_committed_bytes_without_writing(self):
        self.changed_pair()
        guard.verify_worktree(self.repo, self.base)
        candidate = self.commit()
        before = {path: (self.repo / path).read_bytes() for path in guard.SNAPSHOT_PATHS}
        result = self.verify(candidate)
        self.assertTrue(result["changed"])
        self.assertEqual(result["content_sha256"], self.digest())
        self.assertEqual(result["item_count"], 1)
        self.assertEqual(self.head(), candidate)
        self.assertEqual(self.git("status", "--porcelain"), b"")
        self.assertEqual(before, {path: (self.repo / path).read_bytes() for path in guard.SNAPSHOT_PATHS})

    def test_noop_accepts_existing_pair_without_commit(self):
        result = self.verify()
        self.assertFalse(result["changed"])
        self.assertEqual(self.head(), self.base)
        self.assertEqual(self.git("status", "--porcelain"), b"")
        self.git("commit", "--allow-empty", "-m", "Synthetic empty commit")
        with self.assertRaises(guard.GuardError):
            self.verify()

    def test_unstaged_staged_and_untracked_code_fail_worktree_guard(self):
        self.changed_pair()
        readme = self.repo / "README.md"
        readme.write_text("Synthetic unrelated change.\n")
        with self.assertRaises(guard.GuardError):
            guard.verify_worktree(self.repo, self.base)
        self.git("add", "--", "README.md")
        with self.assertRaises(guard.GuardError):
            guard.verify_worktree(self.repo, self.base)
        self.git("restore", "--staged", "--worktree", "--", "README.md")
        (self.repo / "private-fixture.txt").write_text("Synthetic unrelated note.\n")
        with self.assertRaises(guard.GuardError):
            guard.verify_worktree(self.repo, self.base)

    def test_unrelated_committed_code_is_rejected(self):
        self.changed_pair()
        (self.repo / "README.md").write_text("Synthetic unrelated change.\n")
        self.commit("README.md", *guard.SNAPSHOT_PATHS)
        with self.assertRaises(guard.GuardError):
            self.verify()

    def test_hidden_prior_code_commit_and_changed_head_are_rejected(self):
        (self.repo / "README.md").write_text("Synthetic hidden code change.\n")
        self.commit("README.md")
        with self.assertRaises(guard.GuardError):
            guard.verify_worktree(self.repo, self.base)
        self.changed_pair()
        self.commit()
        with self.assertRaises(guard.GuardError):
            self.verify()

    def test_merge_candidate_is_rejected(self):
        self.git("switch", "-c", "side")
        (self.repo / "README.md").write_text("Synthetic side change.\n")
        self.commit("README.md")
        self.git("switch", "main")
        self.changed_pair()
        self.commit()
        self.git("merge", "--no-ff", "side", "-m", "Synthetic merge")
        with self.assertRaises(guard.GuardError):
            self.verify()

    def test_local_replace_refs_cannot_hide_unrelated_changes(self):
        self.changed_pair()
        (self.repo / "README.md").write_text("Synthetic unrelated change.\n")
        bad = self.commit("README.md", *guard.SNAPSHOT_PATHS)
        self.git("reset", "--hard", self.base)
        self.changed_pair()
        good = self.commit()
        self.git("replace", bad, good)
        with self.assertRaises(guard.GuardError):
            self.verify(bad)
        self.assertTrue(self.verify(good)["changed"])

    def test_delete_and_rename_of_snapshot_are_rejected(self):
        self.git("rm", "--", "data/manifest.json")
        self.git("commit", "-m", "Synthetic deletion")
        with self.assertRaises(guard.GuardError):
            self.verify()
        self.git("reset", "--hard", self.base)
        self.git("mv", "data/manifest.json", "data/renamed.json")
        self.git("commit", "-m", "Synthetic rename")
        with self.assertRaises(guard.GuardError):
            self.verify()

    def test_git_symlink_and_submodule_modes_are_rejected(self):
        object_sha = self.git("hash-object", "-w", "--stdin", input_bytes=b"synthetic-target").decode().strip()
        self.git("update-index", "--cacheinfo", "120000," + object_sha + ",data/latest.json")
        self.git("commit", "-m", "Synthetic Git symlink")
        with self.assertRaises(guard.GuardError):
            self.verify(source_hash="0" * 64)
        self.git("reset", "--hard", self.base)
        self.git("update-index", "--cacheinfo", "160000," + self.base + ",data/latest.json")
        self.git("commit", "-m", "Synthetic Git submodule")
        with self.assertRaises(guard.GuardError):
            self.verify(source_hash="0" * 64)

    def test_damaged_nested_manifest_and_future_capture_are_rejected(self):
        manifest_path = self.repo / "data/manifest.json"
        original = manifest_path.read_bytes()
        for modification in ("nested", "future", "count", "duplicate"):
            self.git("reset", "--hard", self.base)
            value = json.loads(original)
            if modification == "nested":
                value["date_bounds"]["earliest"]["notes"] = "Synthetic nonpublic metadata."
            elif modification == "future":
                value["captured_at_utc"] = "2099-01-01T00:00:00Z"
            elif modification == "count":
                value["item_count"] = True
            if modification == "duplicate":
                content = '{"item_count":1,' + json.dumps(value)[1:]
            else:
                content = json.dumps(value)
            manifest_path.write_text(content)
            self.commit("data/manifest.json")
            with self.assertRaises(guard.GuardError):
                self.verify()

    def test_manifest_formatting_only_is_valid_without_changing_source_metadata(self):
        path = self.repo / "data/manifest.json"
        manifest = json.loads(path.read_bytes())
        path.write_text(json.dumps(manifest, indent=4) + "\n")
        self.commit("data/manifest.json")
        result = self.verify()
        self.assertTrue(result["changed"])
        self.assertEqual(result["captured_at_utc"], manifest["captured_at_utc"])

    def test_source_change_and_invalid_identifiers_are_rejected(self):
        self.changed_pair()
        candidate = self.commit()
        with self.assertRaises(guard.GuardError):
            self.verify(candidate, source_hash="0" * 64)
        for invalid in ("main", "x" * 40, self.base + " --unsafe", None):
            with self.assertRaises(guard.GuardError):
                guard.verify_snapshot_candidate(self.repo, invalid, candidate, self.digest())

    def test_error_output_does_not_reveal_git_stderr(self):
        result = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"SYNTHETIC_PRIVATE_DETAIL")
        with mock.patch.object(guard.subprocess, "run", return_value=result), \
             mock.patch.object(guard.sys, "argv", ["guard", "--repo", str(self.repo), "--base", self.base, "--check-worktree"]):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(guard.main(), 1)
        self.assertNotIn("SYNTHETIC_PRIVATE_DETAIL", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
