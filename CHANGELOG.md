# Changelog

## 0.1.1 — 2026-09-27

- Add `agent-usage-pace` as an executable alias so the project name is discoverable through shell command completion.

## 0.1.0 — 2026-09-27

- Combine Cursor, Claude Code, and Codex usage tracking under `usage-pace`.
- Share pace calculations, progress bars, live refresh, and versioned JSON output.
- Add a combined view with independent provider refresh and error handling.
- Retain the three original command names as installed shortcuts.
- Refresh every 20 seconds by default and respect provider rate-limit backoff.
