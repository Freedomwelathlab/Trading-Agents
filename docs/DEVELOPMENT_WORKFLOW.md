# Development Workflow

1. Read root `CLAUDE.md`.
2. Read `docs/PROJECT_CONTEXT.md` only if the task needs project-level
   context, not for every task.
3. Identify the relevant module via `docs/MODULE_MAP.md`.
4. Read `docs/CODE_MAP.md` for that module.
5. Read only the source files the task touches.
6. Plan (for anything non-trivial — a short plan, not a doc).
7. Implement.
8. Run targeted tests (`pytest tests/test_X.py`, not the full suite, for a
   small change).
9. Fix.
10. Run the full suite (`pytest`) plus `ruff check .` and `mypy apps` before
    considering a change done.
11. Update `docs/IMPLEMENTATION_STATUS.md` if something meaningful shipped.
12. Update `docs/ARCHITECTURE.md`, `docs/CODE_MAP.md`, or `docs/DECISIONS.md`
    if the architecture or a significant decision changed. Don't touch them
    for unrelated changes.
13. Move to the next highest-priority task from `docs/IMPLEMENTATION_STATUS.md`.

Don't reread the entire project at every turn — this workflow exists so that
doesn't have to happen.
