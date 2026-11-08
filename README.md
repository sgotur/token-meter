# token-meter

A [Claude Code](https://claude.com/claude-code) hook that shows token usage and estimated cost, per turn and per session. It's a single Python file with no dependencies.

## What it does

- Prints a summary after every finished response (the `Stop` hook): input, output, cache read and cache write tokens, plus estimated cost, for the turn and for the session.
- Optionally prints a one-line update after every tool call (`--per-tool`).
- Optionally adds a permanent status bar at the bottom of Claude Code (`--statusline`) showing model, context use, turn and session tokens, cost, and a tip.
- Gives a contextual tip, for example to run `/compact` when context gets large, or when the session passes your budget.

Example output after a response:

```
[token-meter] Sonnet 5.5 | 3 model calls this turn
  This turn  in 12 | out 1,840 | cache read 54,210 | cache write 3,120 | est. cost $0.0214
  Session    in 40 | out 6,200 | cache read 210,400 | cache write 9,800 | est. cost $0.0871 (11 calls)
  Tip: Name exact files and give a test command in your prompt to cut exploratory reads.
```

### How it works

Claude Code runs the script as a hook and sends a JSON payload on stdin (`session_id`, `transcript_path`, and so on). The script reads `message.usage` from the session transcript, so token counts match what the API reported. Messages streamed over several transcript lines are counted once.

> **Costs are estimates.** They use API list prices. On a Pro or Max plan you aren't billed per token, so read the dollar figure as "API-equivalent".

## Requirements

- Python 3.8 or later (`python3` on your PATH)
- Claude Code

## Install

1. Put `token_meter.py` anywhere, then run one of the following from its folder:

   ```bash
   python3 token_meter.py --install                  # summary after every response (Stop hook)
   python3 token_meter.py --install --per-tool       # also a one-liner after every tool call
   python3 token_meter.py --install --statusline     # also a permanent status bar
   python3 token_meter.py --install --statusline-only  # status bar only; silent Stop hook feeds it
   ```

2. Restart Claude Code.

The installer:

- copies the script to `~/.claude/hooks/token_meter.py`
- merges the hook entries into `~/.claude/settings.json` and saves a backup as `settings.json.bak`
- skips entries that are already installed, so re-running is safe
- leaves an existing `statusLine` alone unless you add `--force`

### Uninstall

```bash
python3 token_meter.py --uninstall
```

This removes only token-meter's entries from `~/.claude/settings.json` (backup: `settings.json.bak`). You can then delete `~/.claude/hooks/token_meter.py` and `~/.claude/token-meter/`.

## Install options

| Flag | Effect |
| --- | --- |
| `--install` | Adds the `Stop` hook (full summary after each response). |
| `--per-tool` | Also adds a `PostToolUse` hook with a one-line update after each tool call. |
| `--statusline` | Also adds the status bar. |
| `--statusline-only` | Status bar only. The Stop hook stays but is silent. |
| `--force` | Replace an existing `statusLine` setting. |
| `--uninstall` | Remove everything token-meter added. |

The status bar has two lines: usage and cost, then a tip with the cost-optimization link. Set `TOKEN_METER_STATUS_LINES=1` for one line.

## Configuration

All environment variables are optional.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TOKEN_METER_MODE` | `message` | `message` shows output inside Claude Code. `tty` writes to `/dev/tty` instead. |
| `TOKEN_METER_BUDGET` | none | Session budget in USD. A tip appears once it is passed, and the status bar cost turns yellow near the limit and red above it. |
| `TOKEN_METER_CONTEXT_WARN` | `150000` | Prompt size in tokens that triggers the `/compact` tip. |
| `TOKEN_METER_COSTS_URL` | Claude Code costs docs | Link shown as the cost-optimization guide. |
| `TOKEN_METER_PROMPT_URL` | Prompt engineering docs | Link shown as the prompting guide. |
| `TOKEN_METER_HOME` | `~/.claude/token-meter` | Directory for per-session state. |
| `TOKEN_METER_STATUS_LINES` | `2` | Set to `1` for a one-line status bar. |
| `NO_COLOR` | unset | Disables colors in the status bar. |

Set these in your shell profile, or under `"env"` in `~/.claude/settings.json`.

### Custom prices

Built-in prices are in the `PRICES` table in the script. To override or add a model, create `~/.claude/token-meter/pricing.json` with USD per million tokens as `[input, cache_write_5m, cache_write_1h, cache_read, output]`:

```json
{
  "sonnet-5-5": [2, 2.5, 4, 0.10, 10]
}
```

Unknown models fall back to `sonnet-5-5` pricing, and their cost is shown with a `~` prefix.

## Files it creates

- `~/.claude/hooks/token_meter.py`: the installed script
- `~/.claude/settings.json`: updated with hook entries (backup at `settings.json.bak`)
- `~/.claude/token-meter/*.json`: small per-session state files, pruned after 30 days

## Troubleshooting

- **Nothing appears:** restart Claude Code after installing, and check that `hooks` in `~/.claude/settings.json` contains a `token_meter.py` command.
- **Status bar not showing:** you may already have a `statusLine`. The installer reports this and leaves it alone. Re-run with `--force` to replace it.
- **Settings JSON error on install:** the installer refuses to touch an unreadable `settings.json`. Fix the JSON and retry.
- **Run it by hand:** `python3 token_meter.py` with no arguments prints the usage help.

The hook never raises errors into Claude Code. If metering fails, it exits silently.
