# Migration SQL fixtures

These SQL files are read at runtime by the early Alembic revisions in
`alembic/versions/`. They are not standalone documentation and must not be
edited after a revision has been released; add a new revision instead.

Later revisions embed their DDL directly in the Python migration file.
