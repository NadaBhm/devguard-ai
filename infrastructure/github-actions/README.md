# Note on this folder

The Software Specifications Book (section 8, folder tree) lists CI/CD
workflow files under `infrastructure/github-actions/` (ci.yml, cd.yml).

However, GitHub Actions **only** discovers and runs workflows located at
`.github/workflows/` in the repository root - it does not read files from
`infrastructure/github-actions/`.

To keep the documented structure while still having a working pipeline:
- The actual, executable workflows live at `.github/workflows/ci.yml` and
  `.github/workflows/cd.yml`.
- This folder is kept as a pointer/reference for anyone following the
  spec book's folder tree, and can hold shared composite actions or
  reusable workflow snippets later if needed.

`cd.yml` builds and publishes backend/frontend images to GHCR on every
push to `master` -- that part is real and needs no configuration. Its
`deploy` job is a deliberate placeholder: it's gated behind a `production`
GitHub Environment that doesn't exist yet (Settings > Environments), and
its one step is a labelled TODO, not a real deployment command. Wiring it
to an actual AWS target is a team decision (account, Terraform state
backend, secrets) -- not something to invent unilaterally in a workflow
file.

If the team prefers, we can instead move the real files here and use
symlinks from `.github/workflows/`, but plain files under
`.github/workflows/` are simpler and avoid symlink issues on Windows.
