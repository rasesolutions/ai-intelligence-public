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

The existing twice-daily Rase publishing workflow dispatches **Refresh public feed** after checking the public website, including completed runs with no new records. The workflow can also be started from this repository's Actions tab. It has no hosting access or website deployment step.

The refresh reads only the fixed public HTTPS feed and validates a strict field and URL whitelist. It does not access research packages, databases, administrative routes, subscribers or private website source. Source failures, malformed data and unknown fields fail closed and retain the previous verified snapshot. JSON and manifest are published together in one Git commit. Repeated identical content creates no additional commit.

For a local refresh with Python 3.12 or later:

```sh
python -m unittest discover -s tests -v
python tools/update_public_snapshot.py --output-dir data
```

`--check` validates without writing. `--input path/to/feed.json` supports offline validation of a previously downloaded public feed. Publish only the two validated data files together, using a normal fast-forward push. Never force-push to resolve concurrent updates.

Rase's original editorial summaries link to supporting evidence. Linked third-party publications retain their respective rights. This repository does not copy third-party articles or imagery.
