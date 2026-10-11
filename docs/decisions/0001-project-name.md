# 0001: Project name and identifiers

- Status: accepted
- Date: 2026-10-09
- Plan ref: D0.1

## Context
The name appears in the package, the command line, imports, the repository and the docs. Changing it after the first release breaks users.

## Decision
- Project: **AI Meterix**
- Repository: `ai-meterix`
- Python package (PyPI): `ai-meterix`; import name `ai_meterix`
- Command line: `aimeterix`

## Consequences
- One spelling everywhere: **meterix**. Before the first release, check that `ai-meterix` is free on PyPI and GitHub.
