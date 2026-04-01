# maxmanager

Credential manager for running multiple Claude Max subscriptions across dev containers.

## Problem

Claude Code authenticates via a single `~/.claude/.credentials.json` file containing OAuth tokens (access + refresh). Access tokens expire every ~6 hours and are silently refreshed by the CLI, which mutates the file in place. Refresh tokens may rotate on each use.

When running 5-6 dev containers simultaneously — each bind-mounting the host's `~/.claude` — all containers share a single credential set and a single Max plan's rate limits. This means:

- **Rate limit contention**: Heavy usage in one container starves the others
- **Refresh token races**: Multiple containers refreshing the same token simultaneously can invalidate it
- **No plan splitting**: Additional Max subscriptions can't be utilized

## Solution

maxmanager is a two-tier credential management system that uses environment variable injection to let multiple containers use different credentials simultaneously without file conflicts.

### Host Manager (Windows)

A Python service running on the Windows host (as a scheduled task or background process) that:

- **Maintains N credential profiles** in `~/.claude-profiles/<name>/`, each with its own `.credentials.json`
- **Proactively refreshes tokens** every ~4 hours (before the 6-hour expiry) by performing the OAuth refresh flow
- **Updates `state.json`** with token freshness metadata so containers can make informed choices
- **Never requires manual interaction** after initial login setup

### Container Agent (Linux)

A lightweight Python CLI that runs inside each dev container at startup or before new conversations:

- **Reads `state.json`** from the shared profiles mount to check rate limit snapshots and allocation locks
- **Selects the optimal credential** — picks the account with the most remaining headroom (lowest `five_hour.used_percentage`)
- **Records its allocation** in `state.json` (container ID + account + timestamp) so other containers can see the distribution — multiple containers may share the same account
- **Exports env vars** (`CLAUDE_CODE_OAUTH_REFRESH_TOKEN`, `CLAUDE_CODE_OAUTH_SCOPES`) so the CLI authenticates via environment, not the shared `.credentials.json`

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Windows Host                                               │
│                                                             │
│  ~/.claude-profiles/                  (bind-mounted to all) │
│  ├── state.json         ← allocation locks + rate snapshots │
│  ├── acct-alice/                                            │
│  │   ├── .credentials.json   ← kept fresh by host manager  │
│  │   └── label.txt           ← "alice@gmail.com"           │
│  ├── acct-bob/                                              │
│  │   ├── .credentials.json   ← kept fresh by host manager  │
│  │   └── label.txt           ← "bob@gmail.com"             │
│  └── acct-.../               ← N profiles supported        │
│                                                             │
│  Host Manager (Python, scheduled task, runs every ~4h)      │
│  └── for each profile:                                      │
│      ├── check expiresAt                                    │
│      ├── if expiring soon → OAuth refresh → save new tokens │
│      └── update state.json with freshness metadata          │
│                                                             │
│  ~/.claude/             ← shared settings, sessions, history│
│                                                             │
│  Bind mounts per devcontainer:                              │
│    ~/.claude-profiles/ → /home/vscode/.claude-profiles/ (ro)│
│    ~/.claude/          → /home/vscode/.claude/              │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│  Container 1       Container 2       ...       Container 6  │
│                                                             │
│  Agent selects     Agent selects               Agent selects│
│  acct-alice        acct-bob                    acct-alice   │
│  ↓                 ↓                           ↓            │
│  Sets env vars:    Sets env vars:              Sets env vars│
│  OAUTH_REFRESH_    OAUTH_REFRESH_              OAUTH_REFRESH│
│  TOKEN=<alice>     TOKEN=<bob>                 TOKEN=<alice>│
│  ↓                 ↓                           ↓            │
│  Claude CLI        Claude CLI                  Claude CLI   │
│  (reads env,       (reads env,                 (reads env,  │
│   ignores file)     ignores file)               ignores file)│
│  5h: 23%           5h: 12%                     5h: 23%     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## How Auth Works

Claude Code CLI resolves credentials in priority order:

1. `CLAUDE_CODE_OAUTH_TOKEN` — static token, inference-only (limited scope)
2. `CLAUDE_CODE_OAUTH_REFRESH_TOKEN` + `CLAUDE_CODE_OAUTH_SCOPES` — **full OAuth refresh via env** (full scope, used by maxmanager)
3. `~/.claude/.credentials.json` — file-based OAuth (default)
4. `ANTHROPIC_API_KEY` — API key auth (Console billing)

maxmanager uses option 2: the container agent reads the selected profile's refresh token and exports it as an env var. The CLI performs its own token refresh from the env var, bypassing `.credentials.json` entirely. This means:

- **No file conflicts**: 6 containers can use different accounts simultaneously
- **Full scope**: Unlike `CLAUDE_CODE_OAUTH_TOKEN` (setup-token), the refresh flow grants all scopes including remote control, MCP, file upload
- **CLI handles refresh**: The CLI exchanges the refresh token for a fresh access token as needed

## Rate Limit Awareness

Claude Code exposes live rate limit data via the statusline JSON feed:

```json
{
  "rate_limits": {
    "five_hour": {
      "used_percentage": 45.2,
      "resets_at": 1775078240
    },
    "seven_day": {
      "used_percentage": 12.8,
      "resets_at": 1775520000
    }
  }
}
```

Two time windows govern Max plan usage:
- **5-hour rolling window**: Primary constraint for burst usage
- **7-day rolling window**: Weekly cap

The container agent uses this data to balance load across accounts. When multiple accounts are available, the agent picks the one with fewer active allocations, then breaks ties by lower `five_hour.used_percentage`. It also factors in `resets_at` — an account at 80% that resets in 10 minutes may be preferable to one at 40% that resets in 4 hours. Rate limit snapshots older than 10 minutes are treated as unknown (0%) to avoid decisions based on stale data.

## State File (`state.json`)

```json
{
  "profiles": {
    "acct-alice": {
      "label": "alice@gmail.com",
      "token_expires_at": 1775078240050,
      "last_refreshed": 1775060000000,
      "rate_limits": {
        "five_hour": { "used_percentage": 23.4, "resets_at": 1775078240, "updated_at": 1775077800000 },
        "seven_day": { "used_percentage": 12.8, "resets_at": 1775520000, "updated_at": 1775077800000 }
      }
    },
    "acct-bob": {
      "label": "bob@gmail.com",
      "token_expires_at": 1775082000000,
      "last_refreshed": 1775064000000,
      "rate_limits": {
        "five_hour": { "used_percentage": 67.1, "resets_at": 1775080000, "updated_at": 1775077900000 },
        "seven_day": { "used_percentage": 31.2, "resets_at": 1775520000, "updated_at": 1775077900000 }
      }
    }
  },
  "allocations": [
    {
      "container_id": "hub_1",
      "account": "acct-alice",
      "claimed_at": 1775070000000,
      "pid": 12345
    },
    {
      "container_id": "hub_2",
      "account": "acct-bob",
      "claimed_at": 1775070500000,
      "pid": 67890
    }
  ]
}
```

## Session Safety

Credential switching is **inherently safe** with the env var approach. The CLI reads `CLAUDE_CODE_OAUTH_REFRESH_TOKEN` once at process startup, exchanges it for an access token, and holds that token in memory for the entire session. Changing env vars or `.credentials.json` after the CLI starts has no effect on the running process.

This means:
- The container agent selects credentials **before** any Claude session starts
- A running session cannot be disrupted by another container's credential operations
- The host manager refreshing tokens on disk does not affect running sessions — they already have their access token in memory

The allocation entry in `state.json` tracks which containers are using which accounts for load balancing purposes — it is not a mutex. Multiple containers may share the same account.

## Credential Selection Points

Credentials are selected at two points:

1. **Container startup** — The agent runs via `postStartCommand`, selects the best account, and writes env exports to `/tmp/maxmanager.env`. Every shell in the container sources this file, so all Claude CLI processes use the assigned account.

2. **Periodic rebalancing** — A background rebalancer runs every hour inside each container, checking whether a better account is available and swapping if safe. See [Mid-Life Credential Rebalancing](#mid-life-credential-rebalancing) below.

## Mid-Life Credential Rebalancing

The rebalancer runs as a background loop (default: every 1 hour, with random jitter of 0-10 min to avoid thundering herd across containers).

### Decision Flow

```
Every ~1 hour (+jitter):
  1. Read state.json → get usage for all accounts
  2. Compare current account usage vs best available account
  3. If delta < 15 percentage points → no swap, wait another hour
  4. If delta >= 15 points → check if CLI is actively processing
  5. If active → wait another hour (do not interrupt)
  6. If idle → swap credentials, update state.json
```

### Activity Detection

The rebalancer uses three signals from `/proc/PID/` to determine if the Claude CLI is actively working or idle:

| Signal | Active | Idle | How |
|---|---|---|---|
| `/proc/PID/io` syscr delta (2s) | >10 | ~1-2 | High I/O = streaming response |
| Child processes (`pgrep -P`) | Present | None | Children = tool execution (Bash, etc.) |
| TCP connections to :443 | ESTABLISHED | None | Connection = mid-API-call |

**All three must be negative** to declare the session idle and safe to swap.

### Swap Mechanics

Since credentials are injected via env vars (read once at CLI process start), a credential swap requires:
1. Write new env exports to `/tmp/maxmanager.env`
2. Update the allocation in `state.json`
3. The **next** Claude CLI process spawned (new conversation via Ctrl+N) picks up the new env vars

The swap does **not** kill running CLI processes. The current session continues on its existing token until it naturally ends. New sessions use the swapped credential.

### Anti-Churn Hysteresis

To prevent ping-ponging between accounts:

- **Minimum delta**: Only swap if the best account's 5h usage is at least **15 percentage points** lower than the current account
- **Cooldown**: After any swap, no further swaps for **2 hours**
- **Directional asymmetry**: If the last swap was A→B, swapping B→A requires a **25-point** delta (higher bar for reversal)
- **Reset awareness**: If the current account's rate limit window resets within **30 minutes**, skip the swap — the problem solves itself

```
should_swap = (
    current_usage - best_usage >= 15
    AND time_since_last_swap >= 2 hours
    AND (not reversing last swap OR delta >= 25)
    AND current_account_resets_in > 30 minutes
)
```

### Coordination Across Containers

Multiple containers running the rebalancing loop could all target the same "best" account. Mitigations:

- **Staggered jitter**: Each container's loop runs at `interval + random(0, 10min)`, spreading evaluations over time
- **Allocation-aware selection**: The agent picks the account with the fewest existing allocations first, then breaks ties by usage — so even if two containers evaluate simultaneously, they see each other's allocations
- **Atomic state updates**: `state.json` writes go through `flock`-based read-modify-write, preventing lost updates

## Credential Lifecycle

```
1. Initial setup (one-time per account, manual):
   - `claude auth login` → `python -m maxmanager.host.manager setup acct-alice`
   - Repeat for each Max subscription: acct-bob, acct-charlie, ...

2. Host manager (runs every ~4 hours, auto-starts with Windows):
   - For each profile directory, check expiresAt
   - If token expires within 2 hours → perform OAuth refresh
   - Write updated tokens back to profile .credentials.json
   - Update state.json with freshness metadata

3. Container startup (automated via postStartCommand):
   - Container agent reads state.json
   - Picks account with most headroom (fewest allocations, lowest 5h usage %)
   - Records allocation in state.json
   - Exports CLAUDE_CODE_OAUTH_REFRESH_TOKEN + CLAUDE_CODE_OAUTH_SCOPES

4. During session:
   - CLI refreshes access token from env var as needed
   - Rate limit data flows via statusline JSON → updates state.json
   - Snapshots older than 10 minutes are treated as stale
   - Rebalancer checks every ~1 hour for better account options

5. Container shutdown:
   - Release allocation in state.json
```

## Autostart

### Host Manager (Windows startup)

The host manager runs as a Windows Scheduled Task that triggers at logon and repeats every 4 hours:

```powershell
# Install (run once from maxmanager/host/):
powershell -ExecutionPolicy Bypass -File install.ps1
```

This creates a task `MaxManager-Refresh` that:
- Runs `python host/manager.py refresh` at user logon
- Repeats every 4 hours while logged in
- Keeps all profile tokens fresh without manual intervention

### Container Agent (devcontainer startup)

Add to each devcontainer's `postStartCommand`:

```jsonc
// .devcontainer/devcontainer.json
{
  "postStartCommand": "bash /workspaces/hub_1/maxmanager/container/init.sh"
}
```

The init script:
1. Runs the agent to select credentials and write env exports to `/tmp/maxmanager.env`
2. Hooks into `.bashrc` / `.zshrc` so every new terminal sources the env file
3. Every Claude CLI process in the container automatically uses the assigned account

## Project Structure

```
maxmanager/
├── README.md
├── host/                    # Windows host manager
│   ├── manager.py           # Token refresh service + profile setup
│   └── install.ps1          # Install as Windows scheduled task
├── container/               # Dev container agent
│   ├── agent.py             # Credential selector + env exporter
│   ├── rebalancer.py        # Background daemon for periodic credential swaps
│   ├── statusline.sh        # Claude Code statusline hook (reports rate limits)
│   └── init.sh              # Devcontainer startup hook (agent + rebalancer)
├── shared/                  # Shared types and state file logic
│   ├── state.py             # state.json read/write with file locking
│   └── oauth.py             # OAuth refresh token exchange
└── tests/
```

## Requirements

- Windows host with WSL2 + Docker Desktop
- Two or more Claude Max subscriptions (separate Anthropic accounts)
- Python 3.10+ (host manager and container agent)
- Dev containers with bind mount: `~/.claude-profiles/`

## Status

Early development. Not yet functional.
