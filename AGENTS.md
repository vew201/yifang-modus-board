# MODUS development

This repository contains source and isolated tests. Never use a contributor checkout as the production data directory. Runtime paths and credentials belong in ignored local configuration.

Before editing, read 开发端维护规范.md. Run relevant tests, and run tests/run_all.py --browser for backend, workflow, storage or integration changes. Update codex-skill/ and codex-release.json when business rules or APIs change; publish the corresponding client rules with the backend. Preserve credentials outside release files.

## Code Review Rules

- All projects have exactly eight fixed stages. A task's completion must never implicitly complete a stage or project. Successful projects require explicit development completion and final payment receipt. Check transitions and migrations, not only labels.
- Protect existing accounts, customer files and data on upgrade. Trash needs a full recovery window; permanent deletion must validate a fresh scope and preserve shared content. Database deletion and durable file cleanup must commit before unlinking files. Never merge production data or personal connection/activation secrets into Git.
- An API or business-rule change must ship matching versioned Codex rules. A review covers the current head SHA only; re-review after changes. For merges, require CI success and a real APPROVED review on that SHA from an authorized person other than the author. No findings, a comment or a reaction is not approval.

For an authorized PR review, inspect the full diff and relevant owning code, verify CI for the same head and current base, then post APPROVE or REQUEST_CHANGES anchored to the inspected commit. After a clean approved review, /modus merge can request the gated merge. Do not claim automatic review, collaborator access or deployment is active until its actual state has been verified.
