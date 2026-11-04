# Contributing

Thanks for taking the time to contribute.

## Before you start

- Search existing issues and pull requests before opening a new one.
- For changes that affect the database schema, public API contracts, billing,
  authentication, or the real-time pipeline, open an issue first so the design
  can be reviewed before implementation.
- Report security issues privately as described in [`SECURITY.md`](SECURITY.md).

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

Fill in the required environment variables (see `README.md`). Tests load the
application settings, so a valid `.env` is required even for unit tests.

## Checks

Run these before opening a pull request:

```bash
python -m pytest -q
python -m ruff check --select F src tests tools alembic
python tools/arch_guard.py
python -m compileall src tests
```

The full lint command used by CI is in `.github/workflows/ci.yml`.

## Code conventions

- Follow the existing layering: routers stay thin, services orchestrate,
  repositories own data access.
- Keep business logic out of routers and SQL out of services.
- Prefer explicit dependencies over module-level singletons.
- Add comments only where the intent is not obvious from the code.
- Do not add fallback paths that silently hide configuration errors. Missing
  required configuration should fail loudly.

## Database changes

- Every schema change needs an Alembic migration under `alembic/versions/`.
- Keep exactly one Alembic head; CI rejects multiple heads.
- Run `python -m alembic upgrade head` locally before submitting.
- Do not edit an already-applied migration; add a new revision.

## Tests

- Add or update tests for behavior changes.
- Do not assert on user-facing prose. Assert on structured values, state
  transitions, and error codes.
- Integration tests that need PostgreSQL follow the pattern in `tests/`.

## Commit messages

Use the project's conventional style with a scope:

```text
feat(voice): add streaming STT reconnect
fix(chat): preserve candidate order after regeneration
docs: document Milvus migration
```

Keep each commit focused on one topic. Do not mix unrelated formatting changes
with behavior changes.

## Pull requests

- Describe the problem and the chosen approach, not only the files changed.
- Link the related issue when one exists.
- Include the commands you ran and their results.
- Note any behavior that is intentionally out of scope.
- Confirm that no secrets, credentials, or production identifiers are included.

By submitting a contribution, you agree that it is licensed under the
AGPL-3.0 license of this repository.
