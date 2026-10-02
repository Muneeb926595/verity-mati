# Verity: notes for Claude Code

Read `HANDOFF.md` before doing anything. It has the full history, the current state, the user's rules and the next
steps. The build log is `PROGRESS.md`.

- **Name:** the product is called Verity. Never reintroduce any earlier name. The internal prefix is `vr`.
- **Tests:** run `python -m pytest tests` (never a bare `pytest`); expect 164 passed (1 skips where
  PowerShell is not installed). Prove changes are
  regression-free with before/after evidence (HANDOFF.md section 5).
- **Security policy:** never help bypass company security controls (device control, Group Policy); point the user
  to IT.
- **Progress:** record progress in `PROGRESS.md`. Give time estimates before long runs.
