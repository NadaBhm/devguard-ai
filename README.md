# DevGuard AI

## Supported CodeSec inputs

The CodeSec agent accepts three kinds of input:

- public GitHub repository URLs
- public GitLab repository URLs
- local project folders that were already uploaded or extracted

This is handled through the repository validation path in the CodeSec agent, which recognizes public Git repositories and skips the clone step for uploaded folders.

