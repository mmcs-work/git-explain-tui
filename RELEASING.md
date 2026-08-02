# Releasing git-explain-tui

This project publishes to PyPI and creates a GitHub Release from a version tag
through GitHub Actions. It uses [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/),
not a long-lived API token. The release workflow is `.github/workflows/release.yml`.
It also attaches standalone executables for macOS (Apple Silicon and Intel) and
Linux x86_64, plus a `SHA256SUMS` file used by `install.sh`.

## One-time PyPI and GitHub setup

1. Create or sign in to your account at [PyPI](https://pypi.org/).
2. In the GitHub repository, open **Settings → Environments → New environment**
   and create `pypi`. Optionally require approval before deployments; this is a
   useful final check before a public release.
3. In PyPI, open **Your projects → Publishing**, then add a **pending trusted
   publisher** with these exact values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `git-explain-tui` |
   | GitHub owner | `mmcs-work` |
   | Repository name | `git-explain-tui` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   A pending publisher does not reserve a name. It permits the first matching
   workflow run to create the project, so do this immediately before releasing.

## Publish a release

1. Confirm the version in `pyproject.toml` is new. PyPI never allows replacing
   an uploaded version.
2. Run the local checks:

   ```bash
   uv run python -m unittest tests.test_suite
   uv build
   ```

3. Commit and push the version change. Then create and push the matching tag:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

4. Open the repository's **Actions** tab and watch **Release**. If the `pypi`
   environment requires approval, approve its publish job. After PyPI succeeds,
   the workflow creates the GitHub Release and attaches the standalone assets.
5. Verify the project page at
   `https://pypi.org/project/git-explain-tui/`, then test an install in a clean
   shell:

   ```bash
   uv tool install git-explain-tui
   git-explain-tui -v
   ```

For a later release, bump the version, update `uv.lock` if needed, and use the
corresponding new tag, for example `v0.1.1`.

## Why this workflow is safe

Only a job triggered by a `v*` tag can publish. Its `id-token: write`
permission lets PyPI verify the repository, workflow file, and `pypi`
environment and issue a short-lived credential. No PyPI password or API key is
stored in this repository or in GitHub Secrets.
