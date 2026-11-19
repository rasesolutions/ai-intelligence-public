"""Boundary checks for the user-authenticated PR completion step."""
import json
import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
import complete_snapshot_pr as completion

BASE = "a" * 40
HEAD = "b" * 40


def pr(probe=False):
    return {"state": "open", "draft": False,
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "base": {"ref": "main", "sha": BASE, "repo": {"full_name": completion.REPOSITORY}},
            "head": {"sha": HEAD, "ref": ("public-feed-probe/" if probe else "public-feed/") + "a" * 12 + "-" + "b" * 12,
                     "repo": {"full_name": completion.REPOSITORY}},
            "commits": 1, "changed_files": 2, "mergeable": True}


def run(**overrides):
    result = {"id": 10, "head_sha": HEAD, "event": "pull_request", "workflow_id": 7,
              "path": completion.WORKFLOW, "pull_requests": [{"number": 2}],
              "status": "completed", "conclusion": "success"}
    result.update(overrides)
    return result


class Completion(unittest.TestCase):
    def test_protection_requires_ci_owner_review_and_no_bot_merge_access(self):
        protection = {
            "enforce_admins": {"enabled": True},
            "required_pull_request_reviews": {"require_code_owner_reviews": True,
                "required_approving_review_count": 0, "require_last_push_approval": False,
                "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []}},
            "required_status_checks": {"strict": True, "checks": [{"context": "validate", "app_id": 15368}]},
            "allow_force_pushes": {"enabled": False}, "allow_deletions": {"enabled": False},
            "restrictions": {"users": [{"login": completion.PUBLISHER}], "teams": [], "apps": []}}
        with mock.patch.object(completion, "api", return_value=protection):
            completion.verify_protection()
        for key, value in (("enforce_admins", {"enabled": False}),
                           ("required_status_checks", {"strict": True, "checks": []}),
                           ("restrictions", {"users": [{"login": completion.PUBLISHER}], "teams": [], "apps": [{"slug": "github-actions"}]})):
            changed = copy.deepcopy(protection)
            changed[key] = value
            with mock.patch.object(completion, "api", return_value=changed):
                with self.assertRaises(completion.CompletionError):
                    completion.verify_protection()
        changed = copy.deepcopy(protection)
        changed['required_pull_request_reviews']['bypass_pull_request_allowances']['apps'] = [{'slug': 'github-actions'}]
        with mock.patch.object(completion, 'api', return_value=changed):
            with self.assertRaises(completion.CompletionError):
                completion.verify_protection()

    def test_only_bot_snapshot_prs_are_eligible(self):
        self.assertFalse(completion.verify_pr(pr(), HEAD, False))
        for key, value in (("draft", True), ("state", "closed"), ("commits", 2), ("changed_files", 3)):
            value_pr = pr()
            value_pr[key] = value
            with self.assertRaises(completion.CompletionError):
                completion.verify_pr(value_pr, HEAD, False)
        value_pr = pr()
        value_pr["user"]["login"] = "another-user"
        with self.assertRaises(completion.CompletionError):
            completion.verify_pr(value_pr, HEAD, False)

    def test_forks_wrong_bases_and_replaced_heads_fail(self):
        for where, key, value in (("base", "ref", "other"), ("head", "sha", BASE),
                                  ("head", "ref", "arbitrary-branch")):
            value_pr = pr()
            value_pr[where][key] = value
            with self.assertRaises(completion.CompletionError):
                completion.verify_pr(value_pr, HEAD, False)
        value_pr = pr()
        value_pr["head"]["repo"]["full_name"] = "another/repo"
        with self.assertRaises(completion.CompletionError):
            completion.verify_pr(value_pr, HEAD, False)

    def test_probe_is_verifiable_but_never_mergeable(self):
        self.assertTrue(completion.verify_pr(pr(True), HEAD, True))
        with self.assertRaises(completion.CompletionError):
            completion.verify_pr(pr(True), HEAD, False)

    def test_files_must_be_modified_snapshot_files(self):
        valid = [{"filename": "data/latest.json", "status": "modified"}]
        with mock.patch.object(completion, "api", return_value=valid):
            completion.verify_files(2)
        for invalid in ([{"filename": "README.md", "status": "modified"}],
                        [{"filename": "data/latest.json", "status": "renamed", "previous_filename": "tools/private.py"}],
                        [{"filename": "data/latest.json", "status": "deleted"}], []):
            with mock.patch.object(completion, "api", return_value=invalid):
                with self.assertRaises(completion.CompletionError):
                    completion.verify_files(2)

    def test_real_pr_ci_identity_is_required(self):
        self.assertTrue(completion.eligible_run(run(), 2, HEAD, 7))
        for overrides in ({"event": "workflow_dispatch"}, {"head_sha": BASE},
                          {"workflow_id": 9}, {"path": "another-workflow.yml"},
                          {"pull_requests": [{"number": 3}]}):
            self.assertFalse(completion.eligible_run(run(**overrides), 2, HEAD, 7))

    def test_approval_targets_only_the_exact_ci_run(self):
        pending = run(status="waiting", conclusion=None)
        responses = [pending, pending, run()]
        calls = []
        def api(route, method="GET", payload=None):
            calls.append((route, method))
            if route == "actions/workflows/validate-public-snapshot.yml":
                return {"id": 7}
            if route.startswith("actions/workflows/"):
                return {"workflow_runs": [responses.pop(0)]}
            if route.endswith("/jobs?per_page=100"):
                return {"jobs": [{"name": "validate", "conclusion": "success"}]}
            return None
        with mock.patch.object(completion, "api", side_effect=api), mock.patch.object(completion.time, "sleep"):
            self.assertEqual(completion.wait_for_ci(2, HEAD, True, 30), 10)
        self.assertEqual([call for call in calls if call[1] == "POST"], [("actions/runs/10/approve", "POST")])
        self.assertFalse(any("reviews" in call[0] for call in calls))

    def test_ci_failures_and_absent_execution_authorization_fail_closed(self):
        for value in (run(status="waiting", conclusion=None), run(conclusion="failure")):
            def api(route, method="GET", payload=None):
                return {"id": 7} if route == "actions/workflows/validate-public-snapshot.yml" else {"workflow_runs": [value]}
            with mock.patch.object(completion, "api", side_effect=api):
                with self.assertRaises(completion.CompletionError):
                    completion.wait_for_ci(2, HEAD, False, 1)

    def test_source_rechecked_after_ci_before_merge(self):
        with mock.patch.object(completion, "command", return_value=json.dumps({"type": "User", "login": completion.PUBLISHER})), \
             mock.patch.object(completion, "verify_protection"), \
             mock.patch.object(completion, "verify_candidate", side_effect=[(pr(), BASE, False), completion.CompletionError("changed")]), \
             mock.patch.object(completion, "wait_for_ci", return_value=10), \
             mock.patch.object(completion, "api") as api:
            with self.assertRaises(completion.CompletionError):
                completion.complete(Path("."), 2, HEAD, True, True)
            api.assert_not_called()

    def test_merge_uses_exact_head_without_bypass(self):
        with mock.patch.object(completion, "command", return_value=json.dumps({"type": "User", "login": completion.PUBLISHER})), \
             mock.patch.object(completion, "verify_protection"), \
             mock.patch.object(completion, "verify_candidate", return_value=(pr(), BASE, False)), \
             mock.patch.object(completion, "wait_for_ci", return_value=10), \
             mock.patch.object(completion, "api", return_value={"merged": True, "sha": "c" * 40}) as api:
            result = completion.complete(Path("."), 2, HEAD, True, True)
            self.assertEqual(result["state"], "merged")
            self.assertEqual(api.call_args.args, ("pulls/2/merge", "PUT", {"sha": HEAD, "merge_method": "merge", "commit_title": "Refresh approved public AI developments"}))

    def test_hosted_bot_authentication_cannot_complete_a_pr(self):
        with mock.patch.object(completion, "command", return_value=json.dumps({"type": "Bot"})):
            with self.assertRaises(completion.CompletionError):
                completion.complete(Path("."), 2, HEAD, True, True)


if __name__ == "__main__":
    unittest.main()
