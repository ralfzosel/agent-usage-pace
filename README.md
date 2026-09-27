# Agent Usage Pace

See whether your coding-agent subscription usage is ahead of or behind the time
elapsed. One terminal command for **Cursor**, **Claude Code**, and **Codex**.

```text
Codex

Codex — 7 days
🟢 BELOW PACE — 15.0 percentage points
Window elapsed   50.0%  ██████████░░░░░░░░░░
Usage            35.0%  ███████░░░░░░░░░░░░░
At this window's average pace: 70.0% at reset
```

*Illustrative output. Actual reset timestamps appear in your local timezone.*

## Install

Requires Python 3.10 or later. There are no third-party runtime dependencies.

```bash
uv tool install git+https://github.com/ralfzosel/agent-usage-pace.git
```

Or use pipx:

```bash
pipx install git+https://github.com/ralfzosel/agent-usage-pace.git
```

## Usage

`agent-usage-pace` is the primary command and matches the project name.

```bash
agent-usage-pace                 # All three providers, live in an interactive terminal
agent-usage-pace --all            # Same as above
agent-usage-pace cursor
agent-usage-pace claude
agent-usage-pace codex
agent-usage-pace --all --once     # One snapshot
agent-usage-pace codex --json     # One JSON snapshot
agent-usage-pace --interval 60 --tolerance 3
```

Default refresh intervals are **20 seconds for Cursor and Codex** and **5 minutes
for Claude**, whose usage endpoint can throttle frequent polling. Press any key
to quit. Piped or redirected output automatically takes one snapshot. Each
provider refreshes independently; one provider's error or rate-limit backoff does
not pause the others. The display updates countdowns every second without making
extra API requests.

`--interval SECONDS` explicitly overrides all providers; `--claude-interval SECONDS`
can set Claude separately. For example, `--interval 20 --claude-interval 600`
checks Cursor/Codex every 20 seconds and Claude every 10 minutes.

The old `usage-pace` name is retained only as a compatibility alias. The
`cursor-usage-pace`, `claude-usage-pace`, and `codex-usage-pace` commands remain
provider-specific compatibility shortcuts; all help text uses `agent-usage-pace`.

| Provider | Authentication | Windows |
| --- | --- | --- |
| Cursor | Existing Cursor desktop login | Monthly billing cycle, with Auto+Composer and API usage breakdown |
| Claude | Existing Claude Code subscription login | Five-hour session and weekly limits, including available model-specific limits |
| Codex | Existing Codex CLI ChatGPT login | Every reported quota window and model bucket |

### Authentication

**Cursor:** sign in to the Cursor desktop app. The script reads its local state
database in read-only mode on macOS, Linux, or Windows. You can also set
`CURSOR_ACCESS_TOKEN` or use `agent-usage-pace cursor --token TOKEN`.

**Claude:** run `claude auth login` with a Claude subscription. On macOS, the
script reads the `Claude Code-credentials` Keychain entry. Elsewhere, it reads
`$CLAUDE_CONFIG_DIR/.credentials.json`, defaulting to `~/.claude/.credentials.json`.
Use `agent-usage-pace claude --credentials-file PATH` for another credentials file.
`CLAUDE_CODE_OAUTH_TOKEN` or `--token TOKEN` can supply an OAuth token with
`user:profile` scope. An API key or inference-only token cannot read subscription
usage. If your token expires, open Claude Code to refresh it or sign in again.

**Codex:** install the Codex CLI and run `codex login` with ChatGPT. The script
starts a private app-server helper and calls
[`account/rateLimits/read`](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt).
Codex manages its own credentials. The helper inherits `CODEX_HOME`, exits after
each fetch, and does not start a chat or model turn. Use `--codex PATH` to select
a different executable and `--timeout SECONDS` to change the default 20-second
network/app-server request timeout.

Credentials are not printed or cached by this package. Environment variables or
native login storage are preferable to command-line tokens, which can enter shell
history. Requests go to the provider's own service; there is no project telemetry.

### Reading the pace

- **Below pace:** usage is more than 2 percentage points below time elapsed.
- **On pace:** usage is within ±2 percentage points of time elapsed.
- **Above pace:** usage is more than 2 percentage points above time elapsed.

Use `--tolerance` to change that band. This is a linear planning aid, not a forecast
of your future workload. Short bursts just after a reset can produce very large
projections; projections are intentionally allowed to exceed 100%.

Cursor uses its API billing-cycle dates when available. `--renew-day` supplies a
fallback day from 1 to 28 (default: 10); estimated dates are marked in the display.
Claude's start times are inferred from its reset times minus five hours or seven
days. Codex supplies its own window lengths. Missing or expired timing data leaves
pace unavailable, and missing usage is never assumed to be zero.

Claude counters are shared with Claude apps. Extra-usage spending, credit balances,
and earned rate-limit resets are outside the pace calculation. Cursor and Claude
use internal endpoints that can change. HTTP rate limits trigger a pause of at
least five minutes for that provider, honoring a longer `Retry-After` value. After
an error, any retained snapshot is explicitly marked as last-known usage.

### JSON and exit codes

JSON has a versioned envelope: `schema_version`, `generated_at`, and a `providers`
object keyed by provider name. Each provider includes its fetch timestamp, error,
stale flag, retry delay, and usage windows with pace calculations. The shortcut
commands use this same format, replacing the original standalone scripts' formats.

Exit status is `0` on success, `1` if any provider fails in a one-shot request,
`2` for invalid arguments, or `130` after Ctrl+C. With `--all`, successful results
are still printed when another provider fails.

### Offline examples

Manual snapshots never read credentials or contact a provider. Supply a reset
timestamp within the chosen window for a meaningful pace calculation:

```bash
agent-usage-pace claude --usage 35 --window seven_day --resets-at '2026-10-01T12:00:00+02:00'
agent-usage-pace codex --usage 25 --window five_hour --resets-at '2026-09-27T16:00:00+02:00'
agent-usage-pace cursor --usage 40 --renew-day 10
```

## Development

```bash
git clone https://github.com/ralfzosel/agent-usage-pace.git
cd agent-usage-pace
uv sync
uv run agent-usage-pace --help
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
uv build
```

Provider authentication and response parsing live in `src/agent_usage_pace/providers`.
Pace calculations, rendering, terminal handling, and CLI scheduling are shared.
Tests use synthetic fixtures and a fake local app server; no provider accounts
or network requests are needed. CI checks supported Python versions, formatting,
and package builds.

## License

MIT. This independent project is not affiliated with Cursor, Anthropic, or OpenAI.
