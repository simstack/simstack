# Pull requests and release notes

Feature and fix PRs normally target `develop`. Release and hotfix PRs target
`main` and use `.github/PULL_REQUEST_TEMPLATE/release.md`, selected with
`?template=release.md` in the PR creation URL.

Use a clear PR title that describes the user-visible changes. For a mixed PR,
mention both the main features and important fixes in its title and summary.
Special commit-message prefixes are not required for GitHub-generated notes.

Before requesting review, add one or more release-note labels:

- `breaking-change`: incompatible API, data, or behavior changes.
- `enhancement`: new features or improvements.
- `bug`: bug fixes.
- `documentation`: documentation changes.
- `dependencies`: dependency updates.
- `maintenance`: internal maintenance, refactoring, or build changes.

Multiple labels are allowed, including existing priority and review-state labels.
Use `skip-changelog` only when the PR should be omitted from release notes; do not
combine it with the six release-note category labels above.

`Release-note labels` checks this policy when a PR is opened, updated, relabeled,
or becomes ready for review. It checks metadata only and does not run PR code.
The workflow must be present on the PR's base branch to run. Making its result a
merge requirement also needs an enforced GitHub required-status-check rule.

Generated release notes list merged PRs rather than every individual change.
Preview the notes using the intended previous and new release tags, and edit the
summary to explain important features, fixes, compatibility, and migration steps.
Tagging, publication, deployment, and production verification happen after merge.
