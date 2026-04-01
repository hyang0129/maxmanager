# maxmanager Architecture

## Overview

maxmanager distributes N Claude Max subscriptions across M dev containers, balancing rate limit usage automatically. Everything runs inside the containers — no Windows service, no host manager, no scheduled tasks.

- **Profiles directory** (Windows host, bind-mounted read-write): Stores credential files for each account. Containers read and write to this directory.
- **Container**: Runs a single `choose_credential` loop — at startup and then periodically — that always picks the best available account given current usage and guards.

## Core Constraint

The Claude Code VS Code extension **does not respect `CLAUDE_CONFIG_DIR`** ([anthropics/claude-code#30538](https://github.com/anthropics/claude-code/issues/30538)). It always reads `~/.claude/.credentials.json`. This means the active credential must be written to `~/.claude/` inside each container.

`CLAUDE_CONFIG_DIR` still works for the standalone CLI, so it is used to probe other accounts' usage in isolation without affecting the active session.

## Data Flow

```
┌─ Windows Host ──────────────────────────────────────────────────┐
│                                                                 │
│  ~/.claude-profiles/                                            │
│  ├── acct-alice/                                                │
│  │   ├── .claude/.credentials.json   ← updated by containers   │
│  │   └── label.txt                                              │
│  ├── acct-bob/                                                  │
│  │   ├── .claude/.credentials.json                              │
│  │   └── label.txt                                              │
│  └── ...N profiles                                              │
│                                                                 │
│  Bind mount: ~/.claude-profiles/ → each container (read-write)  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
        │
        │  bind mount (9p over WSL2, read-write)
        ▼
┌─ Dev Container ─────────────────────────────────────────────────┐
│                                                                 │
│  CREDENTIAL SELECTOR (runs at startup, then every ~1h + jitter) │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ choose_credential():                                     │    │
│  │                                                          │    │
│  │ 1. Discover all profiles in ~/.claude-profiles/          │    │
│  │                                                          │    │
│  │ 2. Probe each profile's usage via CLI:                   │    │
│  │    CLAUDE_CONFIG_DIR=~/.claude-profiles/<acct>/.claude   │    │
│  │    CLI handles token refresh internally, writes tokens   │    │
│  │    back to the profile on the bind mount                 │    │
│  │                                                          │    │
│  │ 3. Evaluate switch triggers (see LOAD_BALANCING.md):     │    │
│  │    - 7d delta >= 15 pts → equalize weekly burn           │    │
│  │    - Current 5hr >= 90% → avoid session interruption     │    │
│  │    Tiebreaker: sha256(hostname) % N                      │    │
│  │                                                          │    │
│  │ 4. Apply guards (skipped on first/startup call):         │    │
│  │    - No trigger fired → no switch                        │    │
│  │    - Within 2h cooldown → skip                           │    │
│  │    - 5hr reset imminent (< 30 min) → skip 5hr trigger    │    │
│  │    - CLI active (I/O, child procs, TCP) → skip           │    │
│  │    - Would reverse last swap direction → +25pt 7d delta  │    │
│  │                                                          │    │
│  │ 5. Copy winner's .credentials.json → ~/.claude/          │    │
│  │    Record active account, timestamp, swap direction      │    │
│  │                                                          │    │
│  │ 6. Sleep ~1h + jitter, repeat                            │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
│  VS CODE EXTENSION                                              │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ Spawns CLI: native-binary/claude (new process per conv)  │    │
│  │   --input-format stream-json                             │    │
│  │   --output-format stream-json                            │    │
│  │                                                          │    │
│  │ CLI reads ~/.claude/.credentials.json at startup         │    │
│  │ If access token expired → auto-refresh using             │    │
│  │   refresh token → writes new tokens back to disk         │    │
│  │ Holds access token in memory for session lifetime        │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Components

### Credential Selector

**Purpose**: Choose the best available credential and activate it. Runs once at container startup, then loops on a ~1h + jitter interval.

**`choose_credential()`**:
1. Discover all profiles in `~/.claude-profiles/`
2. Probe each profile's usage by invoking the CLI with `CLAUDE_CONFIG_DIR` pointing at that profile's `.claude/` directory — the CLI handles token refresh internally and writes updated tokens back to the bind-mounted profile
3. Evaluate switch triggers (see [LOAD_BALANCING.md](LOAD_BALANCING.md)):
   - **7d delta ≥ 15 points**: switch to equalize weekly burn across accounts
   - **Current 5hr ≥ 90%**: switch to avoid interrupting in-progress work; if all accounts are above 90%, pick the lowest 5hr
   - Tiebreaker: `sha256(hostname) % N`
4. Apply guards (skipped on the startup call):
   - No trigger fired → stay on current account
   - Within 2-hour cooldown since last switch → skip
   - Current 5hr reset imminent (< 30 min) → skip the 5hr trigger; wait for reset
   - CLI is active (I/O rate, child processes, TCP connections) → skip
   - Would reverse last swap direction → require 25-point higher 7d delta
5. Copy winner's `.credentials.json` into `~/.claude/.credentials.json`
6. Record active account, timestamp, and swap direction in `/tmp/`

**Startup vs. loop**: Guards are bypassed on the first call — there's no incumbent account to protect, no cooldown to respect, and no running session to disrupt.

## Why Copy Instead of Symlink

Each container needs its own copy of the active credential because:

- The CLI mutates `.credentials.json` on token refresh (writes new access token + possibly new refresh token)
- If all containers symlinked to the same profile file, they'd clobber each other's in-flight refreshes
- A copy gives each container an independently mutable credential

The profile copy on the bind mount is the canonical store. When the CLI refreshes a token during a probe (via `CLAUDE_CONFIG_DIR`), it writes back to the profile — keeping it current for other containers.

## Failure Modes

| Failure | Impact | Recovery |
|---|---|---|
| Access token expired at startup | CLI auto-refreshes using refresh token | Transparent — handled by CLI |
| Two containers refresh same profile simultaneously | Last write wins — both get valid (possibly different) access tokens; one refresh token is wasted | Rare; next probe cycle re-refreshes if needed |
| Refresh token expired or revoked | Profile is broken; manual re-login required | Re-run initial setup for that account |
| Account hits 7d = 100% | Account locked out for the week; throughput reduced to N-1 credentials | Wait for 7d window reset; account is automatically re-eligible |
| Usage probe fails (network) | Can't evaluate triggers | Stays on current account; retries next cycle |
| Container crashes | No cleanup needed | No shared state to corrupt |
| Two containers swap simultaneously | Both write to their own `~/.claude/` | No conflict — each container has its own filesystem |
