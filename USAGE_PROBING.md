# Usage Probing: How It Works

## Problem

maxmanager needs real-time usage percentages (5-hour and 7-day windows) for each Claude Max credential to make switching decisions. This data is not available through any public API or CLI flag.

### Approaches That Don't Work

| Approach | Why it fails |
|----------|-------------|
| Ask the LLM via `claude --print "what's my usage?"` | The model hallucinates numbers — it has no access to account metrics |
| Parse `--output-format json` response | The JSON includes per-request token counts but no cumulative usage percentages |
| `claude auth status --json` | Returns auth info (email, org, plan) but no usage data |
| `/usage` slash command via `--print` mode | Returns "Unknown skill: usage" — interactive-only command |
| `--output-format stream-json` rate_limit_event | Contains `resetsAt` and `status` but no `used_percentage` |
| MITM proxy on `/etc/hosts` | Works for config endpoints, but the actual `/v1/messages` call routes through VS Code's host proxy (`--use-host-proxy`), bypassing the container's DNS |
| Env vars (`HTTPS_PROXY`, `NODE_EXTRA_CA_CERTS`) | The native claude binary ignores standard proxy environment variables |

### Where the Data Lives

The `/usage` slash command in interactive Claude Code sessions displays real usage data fetched from Anthropic's backend:

```
Current session
█████████████████████████████▌   59% used
Resets 11pm (UTC)

Current week (all models)
████████████████▌                33% used
Resets Apr 7, 3pm (UTC)

Current week (Sonnet only)
██████                           12% used
Resets Apr 7, 5pm (UTC)
```

This is the only reliable source of usage percentages accessible from the CLI.

## Solution: Virtual Terminal Probing

We spawn an interactive claude session inside a virtual terminal (PTY), send the `/usage` command, read the rendered screen, and parse the percentages.

### Components

- **pexpect**: Spawns claude in a real PTY so the TUI renders normally. Handles sending keystrokes and reading output.
- **pyte**: A Python terminal emulator that processes ANSI escape sequences and maintains a virtual screen buffer. We feed raw PTY output into pyte and read back clean text lines — no escape code parsing needed.

### Sequence

```
1. pexpect.spawn("claude", ["--dangerously-skip-permissions"])
       ↓
2. Wait for bypass-permissions prompt → press Down + Enter to accept
       ↓
3. Wait for interactive prompt to appear
       ↓
4. Type "/usage" + Enter
       ↓
5. Wait for screen to render (poll for "% used" text)
       ↓
6. Read pyte screen buffer → plain text lines
       ↓
7. Parse percentages with regex: r"(\d+)%\s*used"
       ↓
8. Identify which percentage is which by scanning preceding lines
   for "session" (5-hour), "week" (7-day), "Sonnet" (sonnet-only)
       ↓
9. Write to /tmp/maxmanager_rate_limits.json
       ↓
10. Send Esc + /exit to close the session
```

### Output Format

```json
{
  "five_hour_used_pct": 59,
  "five_hour_resets_at": "11pm (UTC)",
  "seven_day_used_pct": 33,
  "seven_day_resets_at": "Apr 7, 3pm (UTC)",
  "sonnet_week_used_pct": 12,
  "sonnet_week_resets_at": "Apr 7, 5pm (UTC)",
  "timestamp": "2026-04-01T21:03:49+0000"
}
```

### Screen Corruption

The TUI renders progress bars (█), color codes, and overlapping UI elements. When captured through pyte, some lines have garbled text where the autocomplete menu overlaps the usage display. The parser handles this by:

- Anchoring on `(\d+)%\s*used` which survives corruption (the percentage and "used" are always adjacent)
- Looking backward from each match for context words ("session", "week", "Sonnet")
- Looking forward for reset times

### Per-Profile Probing

Each profile has its own `.claude/` config directory. To probe a specific profile:

```python
child = pexpect.spawn(
    "claude",
    args=["--dangerously-skip-permissions"],
    env={**os.environ, "CLAUDE_CONFIG_DIR": str(profile.claude_dir)},
)
```

This makes the CLI authenticate as that profile's account, so `/usage` returns that account's usage.

### Integration with maxmanager

`probe_usage()` in `core.py` should call the virtual terminal probe instead of spawning `claude --print` with a prompt. The returned `UsageSnapshot` maps directly:

| /usage field | UsageSnapshot field | Used by |
|-------------|-------------------|---------|
| `five_hour_used_pct` | `usage_5hr` | 5-hour soft ceiling trigger |
| `seven_day_used_pct` | `usage_7d` | 7-day equalization trigger |
| `five_hour_resets_at` | `reset_at_5hr` | Reset imminent guard |

### Limitations

- **Slow**: Each probe takes ~15–20 seconds (claude startup + TUI render + /usage fetch). With N profiles, total probe time is N × 20s.
- **Fragile**: Depends on the `/usage` screen layout. If Anthropic changes the TUI format, the parser breaks. The regex-based approach is intentionally loose to tolerate minor changes.
- **Resource cost**: Each probe spawns a full claude process. The process is killed after probing, but it does consume a session slot briefly.
- **Bypass permissions**: Requires `--dangerously-skip-permissions` to avoid the interactive permission mode selector. This is acceptable since the probe only runs `/usage` and `/exit`.

### Dependencies

```
pip install pexpect pyte
```

Both are pure Python with no native dependencies.
