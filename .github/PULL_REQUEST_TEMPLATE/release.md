<!--
Use this template only for `release-X.Y.Z` or `hotfix-X.Y.Z` pull requests into
`main`. The package version comes from the post-merge `vX.Y.Z` tag; do not edit
the generated `src/simstack/_version.py` file.
-->

## Release

- Version / tag: `vX.Y.Z`
- Release kind: <!-- regular release or hotfix -->
- Release owner:
- Tracking issue or milestone:

## User-facing summary

<!-- Summarize the value of this release. This text can lead the GitHub release notes. -->

## Compatibility

| Component | Required version or ref | Evidence |
| --- | --- | --- |
| `simstack-server` | | |
| `simstack-ui` | | |
| Python / MongoDB / runner environment | | |

## Risk and recovery

- Breaking API or serialized-data changes:
- Data or file migration requirements:
- Rollback plan:
- Known limitations:

## Publish plan after merge

- Create and push the annotated `vX.Y.Z` tag from the merge commit on `main`.
- Create the GitHub Release using generated notes from merged PRs, categorized by PR labels and `.github/release.yml`.

## Dependent repositories after publishing the tag

- [ ] Update the `simstack` dependency tag to `vX.Y.Z` in `simstack-server/pyproject.toml`, refresh `uv.lock` with `uv lock`, and install with `uv sync --frozen --dev`.
- [ ] Update and install the new `simstack` version in other dependent repositories as needed.
