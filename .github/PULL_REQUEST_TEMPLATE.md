## Problem

<!-- What is broken or missing? Link the related issue. -->

## Approach

<!-- How does this change solve the problem? Call out trade-offs. -->

## Verification

<!-- Commands run and their results. -->

- [ ] `python -m pytest -q`
- [ ] `python -m ruff check --select F src tests tools alembic`
- [ ] `python tools/arch_guard.py`
- [ ] New or changed database migrations run cleanly
- [ ] No secrets, credentials, or production identifiers added

## Out of scope

<!-- Behavior intentionally not changed by this pull request. -->
