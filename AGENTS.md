# Agents — email-mcp

**Scaffolded from:** davidwuest/TEMPLATES unreleased after v2.0.0 (2026-10-09)

## Boundaries — read first
Follow the `boundaries` skill (installed from AGENT-GOVERNANCE) before doing anything in this project. Its no-gos are absolute, not situational — they don't get weighed against the task at hand. If the skill isn't installed, ask before assuming it's safe to proceed without it.

## Stack
Python 3.12, MCP server built on FastMCP from the `mcp` SDK (pinned in `requirements.txt`, must stay <2.0), IMAP/SMTP via the standard library. Shipped as a Docker image on GHCR and run on Kubernetes, served over streamable HTTP.

## Structure
- .agent/specs/<feature-name>/: intent.md → spec.md → plan.md for that feature, written before implementation (see Workflow).
- .agent/MEMORY.md: append-only log of agent decisions/learnings for this project — check it before starting work, append to it as you go
- simple_email_mcp.py: the MCP server (all email actions) — vendored from simple-email-mcp 2.2.0 and developed here — agent can modify
- entrypoint.py: container entrypoint, configures host/port/transport security on the server — agent can modify
- Dockerfile, .github/workflows/: image build and publish to GHCR — ask before changing
- LICENSE-simple-email-mcp: upstream MIT license — never modify

## Workflow
This project is spec-driven: every non-trivial feature goes through `intent.md` → `spec.md` → `plan.md` in `.agent/specs/<feature-name>/` before any code is written. Follow the `feature-workflow` skill (installed from AGENT-GOVERNANCE into `~/.claude/skills/feature-workflow/`); it holds the steps and the templates. If the skill isn't installed, ask before writing any code for a feature.

## Setup
- Install dependencies with `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
- Start the server locally with `MCP_HOST=127.0.0.1 ACCOUNTS_FILE=<path to accounts.json> .venv/bin/python entrypoint.py` (listens on `:8000`, MCP endpoint `/mcp`), or build the image with `docker build -t email-mcp .`.

## Tests
No automated test suite yet. Until there is one, build the image and smoke-test it against a dummy `accounts.json` before committing: MCP `tools/call` of the single tool `email` with arguments `{"action": "validate_config", "params": {}}`. Leaving out `params` returns the action's schema instead of running it.

## Conventions
- Code, comments and commit messages in English; specs in `.agent/specs/` may be in German.
- Changes to `simple_email_mcp.py` go in their own commits, so the diff against the vendored 2.2.0 baseline (commit 78aca44) stays readable.

## Ask before
- Pushing to `main` — CI publishes it as `ghcr.io/davidwuest/email-mcp:latest`.
- Applying anything to the Kubernetes cluster or changing the identity provider configuration.
- Using a real mailbox (reading, sending, moving, flagging) while testing.
