# Repository administration

The intended policy is enforced without a main-branch bypass. Every update uses a PR. Code, documentation, policy and workflow changes require code-owner review and the `validate` CI check. Only the exact snapshot paths in `.github/CODEOWNERS` have no code owner; they still need passing CI. The website remains authoritative for whether a record may be published.

## Main branch

Apply the reviewed `.github/branch-protection.json` configuration to `main`: require PRs and code-owner review, zero general approvals, stale-review dismissal, conversation resolution, and the up-to-date `validate` check from GitHub Actions (app 15368). Enforce for administrators, disable force pushes and deletions, and leave every PR-bypass list empty. Restrict main updates to the existing publisher user `raakesh-blokhra`, with no allowed apps or teams; the bot can propose candidate branches but cannot merge to main. Zero general approvals permits unowned data-only PRs; it does not waive review for owned files. The default CODEOWNERS entry protects every other path, including CODEOWNERS and the branch configuration itself.

For initial setup, first merge the founder-approved bootstrap PR with passing CI, then apply protection immediately. CODEOWNERS must exist on the base branch before GitHub can enforce it. Verify the CODEOWNERS errors API, branch-setting readback, an unmerged data-only probe and an unmerged code-change probe. Never add a temporary broad bypass to work around a bootstrap or review failure.

`@raakesh-blokhra` currently owns code and has repository write access. An author cannot submit an approving review on their own GitHub PR. In the verified zero-general-approvals configuration, a founder-authored fix could merge normally after CI without a self-review submission; a bot-authored PR that also changed README remained blocked for owner review after CI passed. CODEOWNERS does not establish a separate independent reviewer for the owner's own changes. If that additional separation is required, an administrator must establish another eligible code owner or independent PR author. Do not invent a reviewer identity, submit a false approval or add a bypass.

## Actions permissions and completion

Keep default workflow permissions read-only. PR CI is read-only and does not persist checkout authentication. The trusted-main refresh job needs only `contents: write` to publish its candidate branch and `pull-requests: write` to open the data-only PR. These GitHub permissions are repository-scoped; the reviewed Git guards and protected main branch constrain their use. Main push restrictions exclude the bot. The job does not approve reviews, has no Actions-management permission, and receives no PAT, hosting credential or stored secret.

GitHub's repository option **Allow GitHub Actions to create and approve pull requests** must be enabled to allow PR creation. This is a bundled setting and can permit API review calls; the workflow does not call that endpoint, and the bot is not a code owner, so it cannot satisfy the required human code-owner review.

Token-created PR validation can require execution approval. The existing Codex publishing task uses its existing GitHub CLI user authentication to verify the candidate, approve only its exact PR-validation run, follow real `pull_request` CI and merge with an exact-head constraint. No additional stored credential is introduced. Desktop publishing still requires its existing computer/app availability. Dispatched workflow checks are not a substitute for required PR CI.

If the user-authenticated publishing task is unavailable, the data PR remains open and the previous verified snapshot stays public. A maintainer can inspect and complete the same PR using the documented completer; do not create duplicate proposals or bypass protections.

## Security settings and manual follow-up

Repository administrators enable and verify secret scanning and push protection. Both were enabled before this PR. The optional non-provider scanning setting remained disabled on readback despite an accepted API request; its availability or policy cause is unconfirmed and requires organization/GitHub administrator review. Do not enable a paid product or add credentials as an automatic workaround.

After approval, confirm the live administrative settings rather than assuming this document configures GitHub. Record the exact merge, protection readback, first post-merge refresh and probe outcomes in the release report. Preserve the existing snapshot URLs under `main/data/` and existing publishing schedule.

References: [branch protection](https://docs.github.com/en/rest/branches/branch-protection#update-branch-protection), [CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners), [workflow triggering](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow), [required PR checks](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks).
