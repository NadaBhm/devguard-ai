# DevGuard AI

## Supported CodeSec inputs

The CodeSec agent accepts three kinds of input:

- public GitHub repository URLs
- public GitLab repository URLs
- local project folders that were already uploaded or extracted

This is handled through the repository validation path in the CodeSec agent, which recognizes public Git repositories and skips the clone step for uploaded folders.

## Database setup

The project ships a PostgreSQL schema (users, jobs, results, deployments) managed
through SQLAlchemy/Alembic. There are two ways to run it:

- **Docker (recommended):** `docker compose -f infrastructure/docker-compose.yml up postgres`
  publishes Postgres on host port **5433** (not 5432) so it never collides with a
  native/Homebrew instance. Set `DATABASE_URL` to
  `postgresql://devguard:devguard@localhost:5433/devguard` (the default in `.env.example`).
- **Native:** if you run Postgres directly (e.g. Homebrew on macOS), keep port 5432 and
  set `DATABASE_URL` to `postgresql://<your-user>@localhost:5432/devguard`.

`DATABASE_URL` in `.env` overrides the default in `src/backend/config.py` (sqlite dev
fallback). The backend works with either SQLite or Postgres.
