# Rase AI Intelligence — public snapshot

The current, approved public AI developments from [Rase AI Intelligence](https://rasesolutions.com/ai), provided as a small JSON Feed for readers, editorial reviews, newsletters and social-content drafts.

- [Current feed](data/latest.json)
- [Capture manifest](data/manifest.json)
- [Authoritative live feed](https://rasesolutions.com/ai/feed.json)
- [Latest developments on the website](https://rasesolutions.com/ai/latest)

This is the website's bounded recent-development feed: up to 50 developments from the past 90 days, with one milestone per profile. The full directory remains on the website.

## Read the data

Read `data/latest.json` and `data/manifest.json` from the same Git commit. The manifest records the public source URL, capture time, item count and SHA-256 of the saved feed. Commit-pinned reads and the content hash avoid mixing versions during a refresh.

Each item contains the public headline, summary, category, canonical article URL and supporting sources. `external_url`, when present, identifies primary evidence. Original event value, precision and certainty are in `_rase.original_event_date`. `_rase.summary_scope` distinguishes an event summary from profile context. Standard `date_published` is the profile's first publication on Rase, explicitly identified by `_rase.website_publication_scope`. A newly cataloged historical product must not be presented as a new launch.

Capture time describes this copy. It does not change the item's original development or publication dates. Unchanged content produces no new commit; the latest successful **Refresh public feed** workflow run records subsequent checks. If refresh fails, the last verified files remain and the failed run signals that current freshness is unverified.

The live site is authoritative for current publication eligibility. Each refresh replaces the complete current snapshot, so items outside the current public feed disappear from `latest.json`. Absence may reflect the feed's date window or item limit. Git history contains material that was public when captured; refreshing the current copy does not erase historical commits or external caches.

## Refresh

The existing twice-daily Rase publishing workflow dispatches **Refresh public feed** after checking the public website, including completed runs with no new records. The workflow can also be started from this repository's Actions tab. Changed content produces a validated data-only PR; unchanged content produces no commit or PR. The workflow has no hosting access or website deployment step and cannot push directly to `main`.

The refresh reads only the fixed public HTTPS feed and validates a strict field and URL whitelist. It does not access research packages, databases, administrative routes, subscribers or private website source. Source failures, malformed data and unknown fields fail closed and retain the previous verified snapshot. JSON and manifest are published together in one Git commit. Repeated identical content creates no additional commit.

Accepted text is also screened for recognizable credentials, personal information, internal paths and private material. A flagged response is rejected before publication and must be corrected at the public source. These pattern checks supplement the existing publication eligibility rules; they do not replace editorial review or prove that every possible sensitive value can be detected.

For a local refresh with Python 3.12 or later:

```sh
python -m unittest discover -s tests -v
python tools/update_public_snapshot.py --output-dir data
```

`--check` validates without writing. `--input path/to/feed.json` supports offline validation of a previously downloaded public feed. Publication uses a PR; never push directly to `main` or force-push to resolve concurrent updates.

The publisher uses its existing user authentication to finish an exact, verified bot PR:

```sh
python tools/complete_snapshot_pr.py --pr NUMBER --expected-head FULL_SHA --approve-ci --merge
```

The completer checks the authoritative live feed, immutable Git objects, full manifest, current base/head and actual PR CI before merging. It can approve execution of that PR's validation run; it does not approve code reviews. A stale or unexpected proposal fails closed. Reconcile pending proposals before starting another refresh so a retry cannot create a duplicate.

The optional `verification_probe` dispatch input defaults to false. It changes only manifest whitespace, retaining every value and the feed bytes. Its separate `public-feed-probe/` PR can be checked without `--merge`, then closed unmerged. The completer rejects merging a probe.

Changes to the validator, workflow or publication policy require a pull request and owner approval. The reviewed refresh continues updating validated public data automatically. See [Security and emergency withdrawal](SECURITY.md) for private reporting and the procedure for removing material from the current snapshot, historical commits and external copies.

See [Repository administration](ADMIN.md) for the no-bypass branch rules, required CI, token permissions and remaining manual setup considerations.

Rase's original editorial summaries link to supporting evidence. Linked third-party publications retain their respective rights. This repository does not copy third-party articles or imagery.

