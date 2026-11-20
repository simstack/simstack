# Contributing

## Branching

We use [Gitflow](https://nvie.com/posts/a-successful-git-branching-model/).
`develop` collects the next release; `main` contains production releases.

| Work                | Branch                | Start from | Merge into             |
| ------------------- | --------------------- | ---------- | ---------------------- |
| Feature or fix      | `feature-*` / `fix-*` | `develop`  | `develop`              |
| Release preparation | `release-X.Y.Z`       | `develop`  | `main`, then `develop` |
| Production fix      | `hotfix-X.Y.Z`        | `main`     | `main`, then `develop` |

Tag each release or hotfix merge on `main` with an annotated `vX.Y.Z` tag.

## Versioning

Use [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **MAJOR:** incompatible public API changes.
- **MINOR:** backward-compatible features.
- **PATCH:** backward-compatible bug fixes.

## Pull requests

Complete the [PR template](pull_request_template.md) or
[release template](PULL_REQUEST_TEMPLATE/release.md). See the
[README](../README.md) for local setup and project conventions.
