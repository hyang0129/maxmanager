# Plan: Implement core credential selector (#1)

## Summary

Implement the core maxmanager runtime as a FastAPI server with a `choose_credential()` orchestrator that discovers credential profiles, probes their usage, evaluates switch triggers (7d equalization, 5hr soft ceiling), applies guards (cooldown, CLI activity, direction reversal, reset imminence), and copies the winning credential to `~/.claude/.credentials.json`. The server exposes `/health`, `/status`, and `/trigger` endpoints, and runs `choose_credential()` in a background loop via FastAPI lifespan with ~1h + jitter interval. All logging uses loguru exclusively.

## File Structure

```
maxmanager/
  ARCHITECTURE.md              (existing)
  LOAD_BALANCING.md            (existing)
  README.md                    (existing)
  initial_setup_script.ps1     (existing)
  pyproject.toml               (new)
  main.py                      (new — entry point, uvicorn launch, loguru config)
  maxmanager/
    __init__.py                (new)
    models.py                  (new — Profile, UsageSnapshot, SwitchState, Trigger)
    constants.py               (new — thresholds, paths, timing)
    core.py                    (new — choose_credential + all sub-functions)
    server.py                  (new — FastAPI app, lifespan, endpoints)
  tests/
    __init__.py                (new)
    conftest.py                (new — shared fixtures)
    test_core.py               (new)
    test_models.py             (new)
    test_server.py             (new)
```

## Affected Files

| File | Change type | Owned by |
|---|---|---|
| `pyproject.toml` | Create | Coder B |
| `main.py` | Create | Coder B |
| `maxmanager/__init__.py` | Create | Coder A |
| `maxmanager/models.py` | Create | Coder A |
| `maxmanager/constants.py` | Create | Coder A |
| `maxmanager/core.py` | Create | Coder A |
| `maxmanager/server.py` | Create | Coder B |
| `tests/__init__.py` | Create | Tester |
| `tests/conftest.py` | Create | Tester |
| `tests/test_core.py` | Create | Tester |
| `tests/test_models.py` | Create | Tester |
| `tests/test_server.py` | Create | Tester |

## File Ownership Table

| Agent | Files |
|---|---|
| Coder A (core logic) | `maxmanager/__init__.py`, `maxmanager/models.py`, `maxmanager/constants.py`, `maxmanager/core.py` |
| Coder B (server + entry point) | `pyproject.toml`, `main.py`, `maxmanager/server.py` |
| Tester | `tests/__init__.py`, `tests/conftest.py`, `tests/test_core.py`, `tests/test_models.py`, `tests/test_server.py` |

## Data Models

```python
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

@dataclass
class Profile:
    name: str                      # directory name, e.g. "acct-alice"
    label: str                     # from label.txt, or same as name
    path: Path                     # e.g. ~/.claude-profiles/acct-alice
    credentials_path: Path         # e.g. ~/.claude-profiles/acct-alice/.claude/.credentials.json
    claude_dir: Path               # e.g. ~/.claude-profiles/acct-alice/.claude

@dataclass
class UsageSnapshot:
    profile_name: str
    usage_7d: float | None         # 0.0-100.0, None if probe failed
    usage_5hr: float | None        # 0.0-100.0, None if probe failed
    probed_at: datetime = field(default_factory=datetime.utcnow)
    reset_at_5hr: datetime | None = None   # when the 5hr window resets, if known

@dataclass
class Trigger:
    kind: Literal["7d_equalization", "5hr_ceiling"]
    source: str                    # profile_name of current account
    target: str                    # profile_name of recommended switch target
    delta: float                   # usage delta that fired this trigger

@dataclass
class SwitchState:
    active_profile: str
    switched_at: datetime
    direction: tuple[str, str] | None   # (from, to) of last switch
    last_snapshots: list[dict]          # serialized UsageSnapshots
```

## Task List

### Wave 1 (parallel)

- **Task 1.1** — Define models and constants — Coder A — files: `maxmanager/__init__.py`, `maxmanager/models.py`, `maxmanager/constants.py`
- **Task 1.2** — Project scaffolding and server shell — Coder B — files: `pyproject.toml`, `main.py`, `maxmanager/server.py`
- **Task 1.3** — Test fixtures and model tests — Tester — files: `tests/__init__.py`, `tests/conftest.py`, `tests/test_models.py`

### Wave 2 (parallel, depends on Wave 1)

- **Task 2.1** — Implement all core sub-functions — Coder A — files: `maxmanager/core.py`
- **Task 2.2** — Wire server to core — Coder B — files: `maxmanager/server.py`
- **Task 2.3** — Core unit tests — Tester — files: `tests/test_core.py`

### Wave 3 (depends on Wave 2)

- **Task 3.1** — Server integration tests — Tester — files: `tests/test_server.py`

## Acceptance Criteria

- [ ] `discover_profiles()` returns correct `Profile` list from a profiles directory
- [ ] `probe_usage()` invokes CLI with correct `CLAUDE_CONFIG_DIR` and parses output
- [ ] `evaluate_triggers()` returns 7d trigger when delta >= 15
- [ ] `evaluate_triggers()` returns 5hr trigger when current >= 90% and better option exists
- [ ] `evaluate_triggers()` picks lowest 5hr when all above 90%
- [ ] `evaluate_triggers()` returns None when no threshold exceeded
- [ ] `evaluate_triggers()` uses sha256(hostname) % N as tiebreaker
- [ ] `check_guards()` bypasses all guards when `startup=True`
- [ ] `check_guards()` blocks on cooldown < 2h
- [ ] `check_guards()` blocks 5hr trigger when reset imminent (< 30 min)
- [ ] `check_guards()` blocks when CLI is active
- [ ] `check_guards()` blocks direction reversal unless delta >= 25pt premium
- [ ] `activate_credential()` copies (not symlinks) to `~/.claude/.credentials.json`
- [ ] `write_state()` writes valid JSON to `/tmp/maxmanager_state.json`
- [ ] `choose_credential()` correctly orchestrates all sub-functions
- [ ] `GET /health` returns 200
- [ ] `GET /status` returns active account and last usage snapshots
- [ ] `POST /trigger` runs `choose_credential(startup=False)` and returns result
- [ ] Background loop catches exceptions and continues
- [ ] All logging uses loguru; no `print()` or stdlib `logging`

## Open Questions

- **CLI usage command**: The exact CLI command and output format for probing usage is not specified in the issue. `probe_usage()` will be written with a clear parsing interface so the logic can be updated once the command is known. For now, assume a subprocess call returning parseable text with 5hr and 7d percentages.
- **5hr reset timing**: How the 5hr window reset time is determined is unspecified (CLI output vs. state history). `UsageSnapshot` includes an optional `reset_at_5hr` field; the guard will skip if this is populated and within 30 min.
