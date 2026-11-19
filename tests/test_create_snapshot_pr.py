"""Snapshot proposals never grant the hosted job a main-branch publishing path."""
import base64
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
import create_snapshot_pr as proposal
from test_update_public_snapshot import example

BASE = "a" * 40
HEAD = "b" * 40


class Proposals(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        source = self.repo / "source.json"
        source.write_text(json.dumps(example()), encoding="utf-8")
        proposal.snapshot.update(self.repo / "data", source)
        self.contents = proposal.pair(self.repo)
        self.source_sha = hashlib.sha256(self.contents[0]).hexdigest()
        self.branch = proposal.branch_name(BASE, self.contents)
        self.git_calls = []
        self.api_calls = []
        self.changes = True
        self.remote_url = f"https://github.com/{proposal.REPOSITORY}.git"
        self.proposals = []
        self.branch_exists = False
        self.closed = []
        self.main_calls = 0
        self.move_main = False
        self.fetch_head = HEAD
        self.patchers = [
            mock.patch.dict(os.environ, {"GH_TOKEN": "synthetic-test-value"}),
            mock.patch.object(proposal.snapshot, "fetch", return_value=self.contents[0]),
            mock.patch.object(proposal.guard, "verify_worktree"),
            mock.patch.object(proposal.guard, "verify_snapshot_candidate"),
            mock.patch.object(proposal, "git", side_effect=self.git),
            mock.patch.object(proposal, "api", side_effect=self.api),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.temporary.cleanup)

    def pr(self, branch=None, user="github-actions[bot]"):
        return {"number": 7, "state": "open", "user": {"login": user, "type": "Bot"},
                "html_url": "https://untrusted.example/ignored",
                "base": {"ref": "main", "sha": BASE, "repo": {"full_name": proposal.REPOSITORY}},
                "head": {"ref": branch or self.branch, "sha": HEAD,
                         "repo": {"full_name": proposal.REPOSITORY}}}

    def git(self, repo, *arguments):
        self.git_calls.append(arguments)
        if arguments in (("remote", "get-url", "origin"), ("remote", "get-url", "--push", "--all", "origin")):
            return self.remote_url
        if arguments[:2] == ("diff", "--name-only"):
            return "\n".join(proposal.FILES) if self.changes else ""
        if arguments == ("rev-parse", "HEAD"):
            return HEAD
        if arguments == ("rev-parse", "FETCH_HEAD"):
            return self.fetch_head
        return ""

    def api(self, repo, path, method="GET", payload=None, missing=False):
        self.api_calls.append((path, method, payload, missing))
        if path == f"repos/{proposal.REPOSITORY}/git/ref/heads/main":
            self.main_calls += 1
            head = "c" * 40 if self.move_main and self.main_calls > 1 else BASE
            return {"object": {"type": "commit", "sha": head}}
        if path.startswith(f"repos/{proposal.REPOSITORY}/git/ref/heads/public-feed"):
            return {"object": {"type": "commit", "sha": HEAD}} if self.branch_exists or not missing else None
        if "/pulls?state=open" in path:
            return copy.deepcopy(self.proposals)
        if "/pulls?state=all" in path:
            return copy.deepcopy(self.closed)
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1].split("?", 1)[0]
            content = self.contents[proposal.FILES.index(name)]
            blob = hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()
            return {"encoding": "base64", "path": name, "content": base64.b64encode(content).decode(), "sha": blob}
        if path.endswith("/pulls") and method == "POST":
            self.branch_exists = True
            return self.pr(payload["head"])
        raise AssertionError("Unexpected API operation")

    def writes(self):
        return [call for call in self.git_calls if "push" in call or "commit" in call]

    def test_changed_snapshot_proposes_only_data_branch(self):
        result = proposal.propose(self.repo, BASE)
        self.assertEqual(result, {"state": "created", "pr_number": 7,
            "pr_url": f"https://github.com/{proposal.REPOSITORY}/pull/7", "base_sha": BASE, "head_sha": HEAD})
        pushes = [call for call in self.git_calls if "push" in call]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0][-1], f"{HEAD}:refs/heads/{self.branch}")
        self.assertFalse(any("--force" in call or "--force-with-lease" in call for call in pushes))
        self.assertIn(("add", "--", *proposal.FILES), self.git_calls)
        self.assertFalse(any("merge" in path or "approve" in path or "/actions/" in path for path, *_ in self.api_calls))
        proposal.guard.verify_snapshot_candidate.assert_called_with(self.repo, BASE, HEAD, self.source_sha)

    def test_noop_creates_no_branch_or_pr(self):
        self.changes = False
        self.assertEqual(proposal.propose(self.repo, BASE)["state"], "noop")
        self.assertEqual(self.writes(), [])
        self.assertFalse(any(method != "GET" for _, method, *_ in self.api_calls))

    def test_actions_checkout_remote_without_git_suffix_can_propose(self):
        self.remote_url = f"https://github.com/{proposal.REPOSITORY}"
        result = proposal.propose(self.repo, BASE)
        self.assertEqual(result["state"], "created")
        self.assertEqual(len([call for call in self.git_calls if "push" in call]), 1)

    def test_other_hosts_repositories_and_multiple_push_urls_are_rejected(self):
        for value in ("https://github.com/another/repo", "https://evil.example/" + proposal.REPOSITORY,
                      f"https://github.com/{proposal.REPOSITORY}.git\nhttps://evil.example/repo"):
            self.remote_url = value
            with self.assertRaises(proposal.PublicationError):
                proposal.propose(self.repo, BASE)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.api_calls, [])

    def test_unexpected_worktree_is_rejected_before_network_or_writes(self):
        proposal.guard.verify_worktree.side_effect = proposal.guard.GuardError("Safe rejection")
        with self.assertRaises(proposal.guard.GuardError):
            proposal.propose(self.repo, BASE)
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.writes(), [])

    def test_live_source_must_equal_validated_pair(self):
        changed = example()
        changed["items"][0]["title"] = "A corrected public title"
        proposal.snapshot.fetch.return_value = json.dumps(changed).encode()
        with self.assertRaises(proposal.PublicationError):
            proposal.propose(self.repo, BASE)
        self.assertEqual(self.writes(), [])

    def test_manifest_extras_or_bad_capture_fail_before_writes(self):
        manifest = json.loads(self.contents[1])
        manifest["date_bounds"]["private"] = "not for publication"
        (self.repo / proposal.FILES[1]).write_text(json.dumps(manifest))
        with self.assertRaises(proposal.PublicationError):
            proposal.propose(self.repo, BASE)
        self.assertEqual(self.writes(), [])

    def test_exact_existing_bot_proposal_is_reused(self):
        self.proposals = [self.pr()]
        result = proposal.propose(self.repo, BASE)
        self.assertEqual(result["state"], "reused")
        self.assertEqual(self.writes(), [])
        self.assertFalse(any(method == "POST" for _, method, *_ in self.api_calls))

    def test_other_pending_pair_stops_duplicate_proposal(self):
        self.proposals = [self.pr("public-feed/" + "c" * 12 + "-" + "d" * 12)]
        with self.assertRaises(proposal.PendingPublication) as error:
            proposal.propose(self.repo, BASE)
        self.assertEqual(error.exception.result["state"], "pending")
        self.assertEqual(self.writes(), [])

    def test_untrusted_human_fork_or_spoofed_ref_does_not_block_queue(self):
        human = self.pr(user="different-author")
        human["user"]["type"] = "User"
        fork = self.pr()
        fork["head"]["repo"]["full_name"] = "external/example-fork"
        spoof = self.pr()
        spoof["user"]["type"] = "User"
        malformed = self.pr("public-feed/not-a-valid-candidate")
        deleted = self.pr()
        deleted["head"]["repo"] = None
        self.proposals = [human, fork, spoof, malformed, deleted]
        self.assertEqual(proposal.propose(self.repo, BASE)["state"], "created")
        self.assertEqual(len([entry for entry in self.api_calls if entry[1] == "POST"]), 1)

    def test_orphan_branch_is_verified_and_proposed_without_force_or_recommit(self):
        self.branch_exists = True
        self.assertEqual(proposal.propose(self.repo, BASE)["state"], "created")
        self.assertEqual(self.writes(), [])
        proposal.guard.verify_snapshot_candidate.assert_called_with(self.repo, BASE, HEAD, self.source_sha)

    def test_remote_fetch_race_is_rejected(self):
        self.proposals = [self.pr()]
        self.fetch_head = "c" * 40
        with self.assertRaises(proposal.PublicationError):
            proposal.propose(self.repo, BASE)
        self.assertEqual(self.writes(), [])

    def test_main_moving_does_not_create_or_merge_a_pr(self):
        self.move_main = True
        with self.assertRaises(proposal.PublicationError):
            proposal.propose(self.repo, BASE)
        self.assertFalse(any(method == "POST" for _, method, *_ in self.api_calls))
        self.assertFalse(any("HEAD:main" in argument for call in self.git_calls for argument in call))

    def test_rejected_candidate_is_not_pushed(self):
        proposal.guard.verify_snapshot_candidate.side_effect = proposal.guard.GuardError("Safe rejection")
        with self.assertRaises(proposal.guard.GuardError):
            proposal.propose(self.repo, BASE)
        self.assertFalse(any("push" in call for call in self.git_calls))

    def test_closed_proposal_is_not_automatically_reopened(self):
        self.branch_exists = True
        self.closed = [self.pr()]
        with self.assertRaises(proposal.PublicationError):
            proposal.propose(self.repo, BASE)
        self.assertEqual(self.writes(), [])
        self.assertFalse(any(method == "POST" for _, method, *_ in self.api_calls))

    def test_probe_is_marked_and_never_merged(self):
        result = proposal.propose(self.repo, BASE, True)
        self.assertEqual(result["state"], "verification_created")
        posted = [payload for _, method, payload, _ in self.api_calls if method == "POST"][0]
        self.assertTrue(posted["head"].startswith("public-feed-probe/"))
        self.assertIn("close without merging", posted["body"])
        self.assertFalse(any("/merge" in path for path, *_ in self.api_calls))

    def test_base_and_pair_both_determine_stable_branch(self):
        self.assertEqual(proposal.branch_name(BASE, self.contents), self.branch)
        self.assertNotEqual(proposal.branch_name("c" * 40, self.contents), self.branch)
        self.assertNotEqual(proposal.branch_name(BASE, (self.contents[0], self.contents[1] + b"\n")), self.branch)

    def test_cli_never_echoes_process_failure_content(self):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["tool", "--base-sha", BASE]), \
             mock.patch.object(proposal, "propose", side_effect=RuntimeError("PRIVATE SYNTHETIC VALUE")), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(proposal.main(), 1)
        self.assertNotIn("PRIVATE", output.getvalue() + errors.getvalue())
        self.assertEqual(errors.getvalue(), "Snapshot proposal failed.\n")


class Commands(unittest.TestCase):
    def test_api_sends_json_on_stdin_and_does_not_log_failure(self):
        failed = subprocess.CompletedProcess([], 1, "", "PRIVATE SYNTHETIC VALUE")
        with mock.patch.object(proposal, "process", return_value=failed) as command:
            with self.assertRaisesRegex(proposal.PublicationError, "GitHub request failed"):
                proposal.api(Path("."), "repos/example/test/pulls", "POST", {"title": "safe"})
        arguments, _, supplied = command.call_args.args
        self.assertEqual(arguments[-2:], ["--input", "-"])
        self.assertEqual(json.loads(supplied), {"title": "safe"})

    def test_git_uses_no_shell_and_captures_sensitive_process_output(self):
        result = subprocess.CompletedProcess([], 0, "safe", "")
        with mock.patch.object(proposal.subprocess, "run", return_value=result) as command:
            self.assertEqual(proposal.git(Path("."), "rev-parse", "HEAD"), "safe")
        self.assertNotIn("shell", command.call_args.kwargs)
        self.assertTrue(command.call_args.kwargs["capture_output"])
        self.assertEqual(command.call_args.kwargs["timeout"], 90)


class GitIntegration(unittest.TestCase):
    def test_real_commit_and_guards_produce_only_the_snapshot_delta(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "repo"
            repo.mkdir()
            def command(*arguments):
                result = subprocess.run(["git", *arguments], cwd=repo, capture_output=True,
                                        text=True, encoding="utf-8", check=True)
                return result.stdout.strip()
            command("init", "-b", "main")
            command("config", "core.autocrlf", "false")
            command("config", "core.hooksPath", str(root / "empty-hooks"))
            command("remote", "add", "origin", f"https://github.com/{proposal.REPOSITORY}.git")
            source = root / "input.json"
            initial = example()
            initial["items"][0]["title"] = "Previous public title"
            source.write_text(json.dumps(initial), encoding="utf-8")
            proposal.snapshot.update(repo / "data", source)
            (repo / "reviewed.py").write_text("# Reviewed code\n", encoding="utf-8")
            command("add", "--all")
            command("-c", "user.name=Synthetic test", "-c", "user.email=test@example.invalid",
                    "commit", "-m", "Reviewed test base")
            base = command("rev-parse", "HEAD")
            current = example()
            source.write_text(json.dumps(current), encoding="utf-8")
            proposal.snapshot.update(repo / "data", source)
            current_bytes = (repo / proposal.FILES[0]).read_bytes()
            original_process = proposal.process
            pushes = []
            def process(arguments, folder, input_value=None):
                if arguments[0] == "git" and "push" in arguments:
                    pushes.append(arguments)
                    return subprocess.CompletedProcess(arguments, 0, "", "")
                if arguments[:2] == ["git", "fetch"]:
                    (repo / ".git" / "FETCH_HEAD").write_text(command("rev-parse", "HEAD") + "\n")
                    return subprocess.CompletedProcess(arguments, 0, "", "")
                return original_process(arguments, folder, input_value)
            def api(folder, path, method="GET", payload=None, missing=False):
                if path.endswith("/git/ref/heads/main"):
                    return {"object": {"type": "commit", "sha": base}}
                if "/git/ref/heads/public-feed/" in path:
                    return None if missing else {"object": {"type": "commit", "sha": command("rev-parse", "HEAD")}}
                if "/pulls?" in path:
                    return []
                if path.endswith("/pulls") and method == "POST":
                    return {"number": 3, "state": "open", "user": {"login": "github-actions[bot]", "type": "Bot"},
                        "base": {"ref": "main", "sha": base, "repo": {"full_name": proposal.REPOSITORY}},
                            "head": {"ref": payload["head"], "sha": command("rev-parse", "HEAD"),
                                     "repo": {"full_name": proposal.REPOSITORY}}}
                if "/contents/" in path:
                    name = path.split("/contents/", 1)[1].split("?", 1)[0]
                    content = (repo / name).read_bytes()
                    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
                    return {"encoding": "base64", "path": name, "sha": blob,
                            "content": base64.b64encode(content).decode()}
                raise AssertionError("Unexpected integration API operation")
            with mock.patch.dict(os.environ, {"GH_TOKEN": "synthetic-test-value"}), \
                 mock.patch.object(proposal.snapshot, "fetch", return_value=current_bytes), \
                 mock.patch.object(proposal, "process", side_effect=process), \
                 mock.patch.object(proposal, "api", side_effect=api):
                result = proposal.propose(repo, base)
            self.assertEqual(result["state"], "created")
            self.assertEqual(command("rev-parse", "main"), base)
            self.assertEqual(set(command("diff", "--name-only", base, result["head_sha"]).splitlines()), set(proposal.FILES))
            self.assertEqual(command("status", "--porcelain"), "")
            self.assertEqual(len(pushes), 1)
            self.assertTrue(pushes[0][-1].startswith(result["head_sha"] + ":refs/heads/public-feed/"))


if __name__ == "__main__":
    unittest.main()
