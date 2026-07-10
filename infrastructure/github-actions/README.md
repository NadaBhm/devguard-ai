# Note on this folder

The Software Specifications Book (section 8, folder tree) lists CI/CD
workflow files under `infrastructure/github-actions/` (ci.yml, cd.yml).

However, GitHub Actions **only** discovers and runs workflows located at
`.github/workflows/` in the repository root - it does not read files from
`infrastructure/github-actions/`.

To keep the documented structure while still having a working pipeline:
- The actual, executable workflow lives at `.github/workflows/ci.yml`.
- This folder is kept as a pointer/reference for anyone following the
  spec book's folder tree, and can hold shared composite actions or
  reusable workflow snippets later if needed (e.g. `cd.yml` in Sprint 5).

If the team prefers, we can instead move the real files here and use
symlinks from `.github/workflows/`, but plain files under
`.github/workflows/` are simpler and avoid symlink issues on Windows.
