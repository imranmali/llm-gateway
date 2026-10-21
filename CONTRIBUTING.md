# Contributing to llm-gateway

Thanks for helping! This project uses **spec-driven development with
[OpenSpec](https://github.com/Fission-AI/OpenSpec)**: behaviour is described in
`openspec/specs/` and every behaviour change starts as a change proposal.

## Workflow

1. **Propose** — create a change under `openspec/changes/<change-id>/` with
   `proposal.md`, delta specs in `specs/<capability>/spec.md`, an optional
   `design.md` and a `tasks.md` checklist (`/opsx:propose` in Claude Code, or
   `openspec new change <id>`). Validate with `openspec validate <id> --strict`.
2. **Branch** — branch from `main` using a prefix that matches the work:
   `feature/<topic>`, `fix/<topic>`, `chore/<topic>`, `docs/<topic>`.
3. **Implement** — work through `tasks.md`, checking boxes as each task's
   verification passes. Each task group lands its own tests and docs.
4. **Commit** — use [Conventional Commits](https://www.conventionalcommits.org/):
   `feat(routing): ...`, `fix(rate-limiting): ...`, `docs: ...`, `ci: ...`,
   `test: ...`, `chore: ...`. Explain *why* in the body.
5. **Archive** — when all tasks are done, run `openspec archive <change-id>`
   so the deltas are merged into `openspec/specs/`.
6. **Pull request** — open a PR to `main` using the template. CI must be green
   (lint, type-check, tests on 3.11–3.13 with ≥ 85% coverage, OpenSpec strict
   validation and the Docker smoke test) before merging.

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

ruff check src tests && ruff format --check src tests   # lint + format
mypy                                                    # strict type-check
pytest --cov                                            # tests + coverage gate
npx @fission-ai/openspec validate --all --strict        # specs
```

Tests must never call real provider APIs — use the `mock` provider or
`httpx.MockTransport`. Inject clocks/sleepers instead of sleeping.

## Releases

Merges to `main` publish `ghcr.io/imranmali/llm-gateway:edge` (and `sha-<sha>`).
To release `X.Y.Z`:

1. In a PR, set `version` in `pyproject.toml` and `__version__` in
   `src/llm_gateway/__init__.py` to `X.Y.Z` and move the `[Unreleased]`
   entries in `CHANGELOG.md` under `## [X.Y.Z] - <date>`.
2. After it merges, run **Actions → Release → Run workflow** on `main` with
   `version` = `X.Y.Z`. The run executes CI, refuses a version that doesn't
   match `pyproject.toml`, lacks a CHANGELOG section or is already tagged, then
   tags the commit `vX.Y.Z`, publishes the semver and `latest` image tags and
   creates the GitHub release (CHANGELOG section plus generated notes).

Pushing a `vX.Y.Z` tag directly triggers the same publish and release.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
