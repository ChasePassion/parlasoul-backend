# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Use GitHub's private vulnerability reporting:

<https://github.com/ChasePassion/parlasoul-backend/security/advisories/new>

Include:

- a description of the issue and its impact
- affected version, commit, or endpoint
- reproduction steps or a proof of concept
- any suggested mitigation

You will receive an acknowledgment after the report is reviewed. Please allow
time for a fix before public disclosure.

## Scope

In scope:

- authentication and authorization bypasses
- row-level security bypasses
- injection, path traversal, and unsafe deserialization
- exposure of secrets, tokens, or personal data
- denial of service through unbounded resource use
- media upload and presigned URL abuse

Out of scope:

- vulnerabilities in third-party providers (report them upstream)
- issues that require a compromised local environment or physical access
- automated scanner output without a demonstrated impact

## Supported versions

Security fixes are applied to the `main` branch. Older commits and forks are not
maintained.

## Handling secrets

The repository is public. Never commit API keys, signing keys, database
credentials, production hostnames, or customer data. Use `.env` locally and
repository secrets in CI. If a secret is committed by accident, rotate it first,
then remove it from history.
