# maxmanager

Credential manager for running multiple Claude Max subscriptions across dev containers.

## Problem

Claude Code authenticates via `~/.claude/.credentials.json`. When running 5-6 dev containers simultaneously, they all share one credential and one Max plan's rate limits:

- **Rate limit contention**: Heavy usage in one container starves the others
- **No plan splitting**: Additional Max subscriptions can't be utilized

## Solution

maxmanager provides N credential profiles on the host, bind-mounted (read-write) into all containers. Each container independently selects a credential at startup, copies it into its `~/.claude/`, and can swap if rate-limited.

**No Windows service. No host manager. No scheduled tasks.** Containers manage everything independently — token refresh, usage probing, and rebalancing all happen inside the containers themselves.

**No shared mutable state.** No file locking beyond opportunistic profile refresh. No cross-container coordination. Each container reads profile credentials and makes its own decisions.

## Known Limitation: `CLAUDE_CONFIG_DIR`

The Claude CLI supports `CLAUDE_CONFIG_DIR` to redirect the config directory. However, **the VS Code extension does not respect this env var** — it always reads from `~/.claude/`.

See: https://github.com/anthropics/claude-code/issues/30538

This means:

- **For the VS Code extension to work**, the selected credential must be written to `~/.claude/.credentials.json` inside each container
- **`CLAUDE_CONFIG_DIR` is used for probing** — invoking the CLI against another account's profile directory without affecting the active credential

## How It Works

### Credential Selection

`choose_credential()` runs at startup and then every ~1 hour (+ jitter):

1. Discover all profiles in `~/.claude-profiles/`
2. Probe each profile's usage by invoking the CLI with `CLAUDE_CONFIG_DIR` pointing at that profile — the CLI handles token refresh internally and writes updated tokens back to the profile
3. Score profiles: lowest 5-hour utilization wins; container hostname hash as tiebreaker
4. Apply guards (skipped on startup): current usage < 70%, delta < 15 points, cooldown, reset window, CLI activity
5. Copy winner's `.credentials.json` into `~/.claude/`; next conversation picks it up

### Usage Probing via `CLAUDE_CONFIG_DIR`

Each profile is probed by pointing the CLI at its directory:

```bash
# Probe acct-bob's usage from a container currently using acct-alice:
CLAUDE_CONFIG_DIR=~/.claude-profiles/acct-bob/.claude claude <usage-command>
```

This is fully isolated — it reads and refreshes tokens in the target profile only, with no effect on the active `~/.claude/` credential.

### Session Safety

The CLI reads credentials once at process startup and holds tokens in memory. Overwriting `~/.claude/.credentials.json` after startup has no effect on the running session. A running session is never disrupted.

## Requirements

- Windows host with WSL2 + Docker Desktop
- Two or more Claude Max subscriptions (separate accounts)
- Dev containers with read-write bind mount to `~/.claude-profiles/`
- **Claude CLI installed via npm** (`npm install -g @anthropic-ai/claude-code`) — the VS Code extension binary is not sufficient; `claude-usage-plz` (used for usage probing) requires the npm-installed CLI

## Status

Early development.
